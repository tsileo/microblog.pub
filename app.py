import binascii
import hashlib
import json
import urllib
import os
import mimetypes
import logging
from functools import wraps
from datetime import datetime

import timeago
import bleach
import mf2py
import pymongo
import piexif
from bson.objectid import ObjectId
from flask import Flask
from flask import abort
from flask import request
from flask import redirect
from flask import Response
from flask import render_template
from flask import session
from flask import url_for
from html2text import html2text
from itsdangerous import JSONWebSignatureSerializer
from itsdangerous import BadSignature
from passlib.hash import bcrypt
from u2flib_server import u2f
from urllib.parse import urlparse, urlencode
from werkzeug.utils import secure_filename

import activitypub
import config
from activitypub import ActivityTypes
from activitypub import clean_activity
from utils.content_helper import parse_markdown
from config import KEY
from config import DB
from config import ME
from config import ID
from config import DOMAIN
from config import USERNAME
from config import BASE_URL
from config import ACTOR_SERVICE
from config import OBJECT_SERVICE
from config import PASS
from config import HEADERS
from config import VERSION
from config import custom_cache_purge_hook
from utils.httpsig import HTTPSigAuth, verify_request
from utils.key import get_secret_key
from utils.webfinger import get_remote_follow_template
from utils.webfinger import get_actor_url

 
app = Flask(__name__)
app.secret_key = get_secret_key('flask')

# Hook up Flask logging with gunicorn
gunicorn_logger = logging.getLogger('gunicorn.error')
root_logger = logging.getLogger()
root_logger.handlers = gunicorn_logger.handlers
root_logger.setLevel(gunicorn_logger.level)

JWT_SECRET = get_secret_key('jwt')
JWT = JSONWebSignatureSerializer(JWT_SECRET)

with open('config/jwt_token', 'wb+') as f:
    f.write(JWT.dumps({'type': 'admin_token'}))  # type: ignore

SIG_AUTH = HTTPSigAuth(ID+'#main-key', KEY.privkey)


def verify_pass(pwd):
        return bcrypt.verify(pwd, PASS)

@app.context_processor
def inject_config():
        return dict(
            microblogpub_version=VERSION,
            config=config,
            logged_in=session.get('logged_in', False),
        )

@app.after_request
def set_x_powered_by(response):
    response.headers['X-Powered-By'] = 'microblog.pub'
    return response

# HTML/templates helper
ALLOWED_TAGS = [
    'a',
    'abbr',
    'acronym',
    'b',
    'blockquote',
    'code',
    'pre',
    'em',
    'i',
    'li',
    'ol',
    'strong',
    'ul',
    'span',
    'div',
    'p',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
]


def clean_html(html):
    return bleach.clean(html, tags=ALLOWED_TAGS)


@app.template_filter()                              
def quote_plus(t):                  
    return urllib.parse.quote_plus(t)                         


@app.template_filter()                              
def clean(html):                                    
    return clean_html(html)                         


@app.template_filter()                              
def html2plaintext(body):                           
    return html2text(body)


@app.template_filter()
def domain(url):
    return urlparse(url).netloc


@app.template_filter()
def get_actor(url):
    if not url:
        return None
    print(f'GET_ACTOR {url}')
    return ACTOR_SERVICE.get(url)

@app.template_filter()
def format_time(val):
    if val:
        return datetime.strftime(datetime.strptime(val, '%Y-%m-%dT%H:%M:%SZ'), '%B %d, %Y, %H:%M %p')
    return val


@app.template_filter()
def format_timeago(val):
    if val:
        try:
            return timeago.format(datetime.strptime(val, '%Y-%m-%dT%H:%M:%SZ'), datetime.utcnow())
        except:
            return timeago.format(datetime.strptime(val, '%Y-%m-%dT%H:%M:%S.%fZ'), datetime.utcnow())
            
    return val

def _is_img(filename):
    filename = filename.lower()
    if (filename.endswith('.png') or filename.endswith('.jpg') or filename.endswith('.jpeg') or
            filename.endswith('.gif') or filename.endswith('.svg')):
        return True
    return False

@app.template_filter()
def not_only_imgs(attachment):
    for a in attachment:
        if not _is_img(a['url']):
            return True
    return False

@app.template_filter()
def is_img(filename):
    return _is_img(filename)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def _api_required():
    if session.get('logged_in'):
        return

    # Token verification
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.form.get('access_token', '')

    # Will raise a BadSignature on bad auth
    payload = JWT.loads(token)


def api_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            _api_required()
        except BadSignature:
            abort(401)

        return f(*args, **kwargs)
    return decorated_function


def jsonify(**data):
    if '@context' not in data:
        data['@context'] = config.CTX_AS
    return Response(
        response=json.dumps(data),
        headers={'Content-Type': 'application/json' if app.debug else 'application/activity+json'},
    )


def is_api_request():
    h = request.headers.get('Accept')
    if h is None:
        return False
    h = h.split(',')[0]
    if h in HEADERS or h == 'application/json':
        return True
    return False

# App routes 

#######
# Login

@app.route('/logout')
@login_required
def logout():
    session['logged_in'] = False
    return redirect('/')


@app.route('/login', methods=['POST', 'GET'])
def login():
    devices = [doc['device'] for doc in DB.u2f.find()]
    u2f_enabled = True if devices else False
    if request.method == 'POST':
        pwd = request.form.get('pass')
        if pwd and verify_pass(pwd):
            if devices:
                resp = json.loads(request.form.get('resp'))
                print(resp)
                try:
                    u2f.complete_authentication(session['challenge'], resp)
                except ValueError as exc:
                    print('failed', exc)
                    abort(401)
                    return
                finally:
                    session['challenge'] = None

            session['logged_in'] = True
            return redirect(request.args.get('redirect') or '/admin')
        else:
            abort(401)

    payload = None
    if devices:
        payload = u2f.begin_authentication(ID, devices)
        session['challenge'] = payload

    return render_template(
        'login.html',
        u2f_enabled=u2f_enabled,
        me=ME,
        payload=payload,
    )


@app.route('/remote_follow', methods=['GET', 'POST'])
@login_required
def remote_follow():
    if request.method == 'GET':
        return render_template('remote_follow.html')

    return redirect(get_remote_follow_template('@'+request.form.get('profile')).format(uri=ID))


@app.route('/authorize_follow', methods=['GET', 'POST'])
@login_required
def authorize_follow():
    if request.method == 'GET':
        return render_template('authorize_remote_follow.html', profile=request.args.get('profile'))

    actor = get_actor_url(request.form.get('profile'))
    if not actor:
        abort(500)
    if DB.following.find({'remote_actor': actor}).count() > 0:
        return redirect('/following')

    follow = activitypub.Follow(object=actor)
    follow.post_to_outbox()
    return redirect('/following')


@app.route('/u2f/register', methods=['GET', 'POST'])
@login_required
def u2f_register():
    # TODO(tsileo): ensure no duplicates
    if request.method == 'GET':
        payload = u2f.begin_registration(ID)
        session['challenge'] = payload
        return render_template(
            'u2f.html',
            payload=payload,
        )
    else:
        resp = json.loads(request.form.get('resp'))
        device, device_cert = u2f.complete_registration(session['challenge'], resp)
        session['challenge'] = None
        DB.u2f.insert_one({'device': device, 'cert': device_cert})
        return ''

#######
# Activity pub routes

@app.route('/')
def index():
    print(request.headers.get('Accept'))
    if is_api_request():
        return jsonify(**ME)

    # FIXME(tsileo): implements pagination, also for the followers/following page
    limit = 50
    q = {
        'type': 'Create',
        'activity.object.type': 'Note',
        'meta.deleted': False,
    }
    c = request.args.get('cursor')
    if c:
        q['_id'] = {'$lt': ObjectId(c)}

    outbox_data = list(DB.outbox.find({'$or': [q, {'type': 'Announce', 'meta.undo': False}]}, limit=limit).sort('_id', -1))
    cursor = None
    if outbox_data and len(outbox_data) == limit:
        cursor = str(outbox_data[-1]['_id'])

    for data in outbox_data:
        if data['type'] == 'Announce':
            print(data)
            if data['activity']['object'].startswith('http'):
                data['ref'] = {'activity': {'object': OBJECT_SERVICE.get(data['activity']['object'])}, 'meta': {}}


    return render_template(
        'index.html',
        me=ME,
        notes=DB.inbox.find({'type': 'Create', 'activity.object.type': 'Note', 'meta.deleted': False}).count(),
        followers=DB.followers.count(),
        following=DB.following.count(),
        outbox_data=outbox_data,
        cursor=cursor,
    )


@app.route('/note/<note_id>')                       
def note_by_id(note_id):                            
    data = DB.outbox.find_one({'id': note_id, 'meta.deleted': False})                                    
    if not data:                                    
        return Response(status=404)                 

    replies = list(DB.inbox.find({                       
        'type': 'Create',                           
        'activity.object.inReplyTo': data['activity']['object']['id'],                                   
        'meta.deleted': False,                      
    }))

    # Check for "replies of replies"
    others = []
    for rep in replies:
        for rep_reply in rep.get('meta', {}).get('replies', []):
            others.append(rep_reply['id'])

    if others:
        # Fetch the latest versions of the "replies of replies"
        replies2 = list(DB.inbox.find({
            'activity.id': {'$in': others},    
        }))
        
        replies.extend(replies2)

        replies2 = list(DB.outbox.find({
            'activity.id': {'$in': others},    
        }))
        
        replies.extend(replies2)


        # Re-sort everything
        replies = sorted(replies, key=lambda o: o['activity']['object']['published'])


    return render_template('note.html', me=ME, note=data, replies=replies)                


@app.route('/.well-known/webfinger')
def webfinger():
    """Enable WebFinger support, required for Mastodon interopability."""
    resource = request.args.get('resource')
    if resource not in ["acct:"+USERNAME+"@"+DOMAIN, ID]:
        abort(404)

    out = {
        "subject": "acct:"+USERNAME+"@"+DOMAIN,
        "aliases": [ID],
        "links": [
            {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html", "href": BASE_URL},
            {"rel": "self", "type": "application/activity+json", "href": ID},
            {"rel":"http://ostatus.org/schema/1.0/subscribe","template": BASE_URL+"/authorize_follow?profile={uri}"},
        ],
    }

    return Response(
        response=json.dumps(out),
        headers={'Content-Type': 'application/jrd+json; charset=utf-8' if not app.debug else 'application/json'},
    )

@app.route('/outbox', methods=['GET', 'POST'])      
def outbox():                                       
    if request.method == 'GET':                     
        if not is_api_request():                    
            abort(404)                              
        # TODO(tsileo): filter the outbox if not authenticated
        # FIXME(tsileo): filter deleted, add query support for build_ordered_collection
        q = {
            'meta.deleted': False,
            'type': {'$in': [ActivityTypes.CREATE.value, ActivityTypes.ANNOUNCE.value]},
        }
        return jsonify(**activitypub.build_ordered_collection(
            DB.outbox,
            q=q,
            cursor=request.args.get('cursor'),
            map_func=lambda doc: clean_activity(doc['activity']),
        ))

    # Handle POST request
    try:
        _api_required()
    except BadSignature:
        abort(401)
 
    data = request.get_json(force=True)
    print(data)
    activity = activitypub.parse_activity(data)

    if activity.type_enum == ActivityTypes.NOTE:
        activity = activity.build_create()

    activity.post_to_outbox()

    # Purge the cache if a custom hook is set, as new content was published
    custom_cache_purge_hook()

    return Response(status=201, headers={'Location': activity.id})


@app.route('/outbox/<item_id>')
def outbox_detail(item_id):
    doc = DB.outbox.find_one({'id': item_id, 'meta.deleted': False})
    return jsonify(**clean_activity(doc['activity']))


@app.route('/outbox/<item_id>/activity')
def outbox_activity(item_id):
    data = DB.outbox.find_one({'id': item_id, 'meta.deleted': False})
    if not data:
        abort(404)
    obj = data['activity']
    if obj['type'] != ActivityTypes.CREATE.value:
        abort(404)
    return jsonify(**clean_activity(obj['object']))

@app.route('/admin', methods=['GET'])
@login_required
def admin():
    q = {
        'meta.deleted': False,
        'meta.undo': False,
        'type': ActivityTypes.LIKE.value,
    }
    col_liked = DB.outbox.count(q)

    return render_template(
            'admin.html',
            instances=list(DB.instances.find()),
            inbox_size=DB.inbox.count(),
            outbox_size=DB.outbox.count(),
            object_cache_size=DB.objects_cache.count(),
            actor_cache_size=DB.actors_cache.count(),
            col_liked=col_liked,
            col_followers=DB.followers.count(),
            col_following=DB.following.count(),
    )
 

@app.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    if request.method == 'POST':
        reply = None
        if request.form.get('reply'):
            reply = activitypub.parse_activity(OBJECT_SERVICE.get(request.form.get('reply')))
        source = request.form.get('content')
        content, tags = parse_markdown(source)       
        to = request.form.get('to')
        cc = [ID+'/followers']
        if reply:
            cc.append(reply.attributedTo)
        for tag in tags:
            if tag['type'] == 'Mention':
                cc.append(tag['href'])

        note = activitypub.Note(                                    
            cc=cc,                       
            to=[to if to else config.AS_PUBLIC],
            content=content,  # TODO(tsileo): handle markdown
            tag=tags,
            source={'mediaType': 'text/markdown', 'content': source},
            inReplyTo=reply.id if reply else None
        )

        create = note.build_create()
        print(create.to_dict())
        create.post_to_outbox()
    
    reply_id = None
    content = ''
    if request.args.get('reply'):
        reply = activitypub.parse_activity(OBJECT_SERVICE.get(request.args.get('reply')))
        reply_id = reply.id
        actor = reply.get_actor()
        domain = urlparse(actor.id).netloc
        content = f'@{actor.preferredUsername}@{domain} '

    return render_template('new.html', reply=reply_id, content=content)


@app.route('/notifications')
@login_required
def notifications():
    # FIXME(tsileo): implements pagination, also for the followers/following page
    limit = 50
    q = {
        'type': 'Create',
        'activity.object.tag.type': 'Mention',
        'activity.object.tag.name': f'@{USERNAME}@{DOMAIN}',
        'meta.deleted': False,
    }
    # TODO(tsileo): also include replies via regex on Create replyTo
    q = {'$or': [q, {'type': 'Follow'}, {'type': 'Accept'}, {'type': 'Undo', 'activity.object.type': 'Follow'}, 
        {'type': 'Announce', 'activity.object': {'$regex': f'^{BASE_URL}'}},
        {'type': 'Create', 'activity.object.inReplyTo': {'$regex': f'^{BASE_URL}'}},
        ]}
    print(q)
    c = request.args.get('cursor')
    if c:
        q['_id'] = {'$lt': ObjectId(c)}

    outbox_data = list(DB.inbox.find(q, limit=limit).sort('_id', -1))
    cursor = None
    if outbox_data and len(outbox_data) == limit:
        cursor = str(outbox_data[-1]['_id'])

    # TODO(tsileo): fix the annonce handling, copy it from /stream
    #for data in outbox_data:
    #    if data['type'] == 'Announce':
    #        print(data)
    #        if data['activity']['object'].startswith('http') and data['activity']['object'] in objcache:
    #            data['ref'] = {'activity': {'object': objcache[data['activity']['object']]}, 'meta': {}}
    #            out.append(data)
    #    else:
    #        out.append(data)

    return render_template(
        'stream.html',
        inbox_data=outbox_data,
        cursor=cursor,
    )

@app.route('/ui/boost')
@login_required
def ui_boost():
    oid = request.args.get('id')
    obj = activitypub.parse_activity(OBJECT_SERVICE.get(oid))
    announce = obj.build_announce()
    announce.post_to_outbox()
    return redirect(request.args.get('redirect'))

@app.route('/ui/like')
@login_required
def ui_like():
    oid = request.args.get('id')
    obj = activitypub.parse_activity(OBJECT_SERVICE.get(oid))
    like = obj.build_like()
    like.post_to_outbox()
    return redirect(request.args.get('redirect'))

@app.route('/ui/undo')
@login_required
def ui_undo():
    oid = request.args.get('id')
    doc =DB.outbox.find_one({'id': oid})
    if doc:
        obj = activitypub.parse_activity(doc.get('activity'))
        undo = obj.build_undo()
        undo.post_to_outbox()
    return redirect(request.args.get('redirect'))

@app.route('/stream')
@login_required
def stream():
    # FIXME(tsileo): implements pagination, also for the followers/following page
    limit = 100
    q = {
        'type': 'Create',
        'activity.object.type': 'Note',
        'activity.object.inReplyTo': None,
        'meta.deleted': False,
    }
    c = request.args.get('cursor')
    if c:
        q['_id'] = {'$lt': ObjectId(c)}

    outbox_data = list(DB.inbox.find(
        {
            '$or': [
                q,
                {
                    'type': 'Announce',
                },
            ]
        }, limit=limit).sort('activity.published', -1))
    cursor = None
    if outbox_data and len(outbox_data) == limit:
        cursor = str(outbox_data[-1]['_id'])

    out = []
    objcache = {}
    cached = list(DB.objects_cache.find({'meta.part_of_stream': True}, limit=limit*3).sort('meta.announce_published', -1))
    for c in cached:
        objcache[c['object_id']] = c['cached_object']
    for data in outbox_data:
        if data['type'] == 'Announce':
            if data['activity']['object'].startswith('http') and data['activity']['object'] in objcache:
                data['ref'] = {'activity': {'object': objcache[data['activity']['object']]}, 'meta': {}}
                out.append(data)
            else:
                print('OMG', data)
        else:
            out.append(data)
    return render_template(
        'stream.html',
        inbox_data=out,
        cursor=cursor,
    )


@app.route('/inbox', methods=['GET', 'POST'])       
def inbox():                                        
    if request.method == 'GET':                     
        if not is_api_request():                    
            abort(404)                              
        try:
            _api_required()
        except BadSignature:
            abort(404)

        return jsonify(**activitypub.build_ordered_collection(  
            DB.inbox,                               
            q={'meta.deleted': False},              
            cursor=request.args.get('cursor'),      
            map_func=lambda doc: doc['activity'],   
        ))                                          

    data = request.get_json(force=True)             
    print(data)                                     
    try:                                            
        print(verify_request(ACTOR_SERVICE))        
    except Exception:                                         
        print('failed to verify request, trying to verify the payload by fetching the remote')
        try:
            data = OBJECT_SERVICE.get(data['id'])
        except Exception:
            print(f'failed to fetch remote id at {data["id"]}')
            abort(422)

    activity = activitypub.parse_activity(data)                 
    print(activity)                                 
    activity.process_from_inbox()                   

    return Response(                                
        status=201,                                 
    )                                               


@app.route('/api/upload', methods=['POST'])
@api_required
def api_upload():
    file = request.files['file']
    rfilename = secure_filename(file.filename)
    prefix = hashlib.sha256(os.urandom(32)).hexdigest()[:6]
    mtype = mimetypes.guess_type(rfilename)[0]
    filename = f'{prefix}_{rfilename}'
    file.save(os.path.join('static', 'media',  filename))

    # Remove EXIF metadata
    if filename.lower().endswith('.jpg') or filename.lower().endswith('.jpeg'):
        piexif.remove(os.path.join('static', 'media',  filename))

    print('upload OK')
    print(filename)
    attachment = [
        {'mediaType': mtype,         
         'name': rfilename,                                    
         'type': 'Document',                              
         'url': BASE_URL + f'/static/media/{filename}'
         },
    ]
    print(attachment)
    content = request.args.get('content')           
    to = request.args.get('to')                     
    note = activitypub.Note(                                    
        cc=[ID+'/followers'],                       
        to=[to if to else config.AS_PUBLIC],
        content=content,  # TODO(tsileo): handle markdown
        attachment=attachment,
    )
    print('post_note_init')
    print(note)
    create = note.build_create()
    print(create)
    print(create.to_dict())
    create.post_to_outbox()
    print('posted')
    
    return Response(
        status=201,
        response='OK',
    )


@app.route('/api/new_note')                         
@api_required                                      
def api_new_note(): 
    source = request.args.get('content')
    if not source:
        raise ValueError('missing content')
    content, tags = parse_markdown(source)       
    to = request.args.get('to')
    cc = [ID+'/followers']
    for tag in tags:
        if tag['type'] == 'Mention':
            cc.append(tag['href'])

    note = activitypub.Note(                                    
        cc=cc,                       
        to=[to if to else config.AS_PUBLIC],
        content=content,  # TODO(tsileo): handle markdown
        tag=tags,
        source={'mediaType': 'text/markdown', 'content': source},
    )
    create = note.build_create()
    create.post_to_outbox()
    return Response(
        status=201,
        response='OK',
    )

@app.route('/api/stream')
def api_stream():
    return Response(
        response=json.dumps(activitypub.build_inbox_json_feed('/api/stream', request.args.get('cursor'))),
        headers={'Content-Type': 'application/json'},
    )

@app.route('/api/follow')
@api_required
def api_follow():
    actor = request.args.get('actor')
    if not actor:
        raise ValueError('missing actor')
    if DB.following.find({'remote_actor': actor}).count() > 0:
        return Response(status=201)

    follow = activitypub.Follow(object=actor)
    follow.post_to_outbox()
    return Response(
        status=201,
    )


@app.route('/followers')                            
def followers():                                    
    if is_api_request():                            
        return jsonify(
            **activitypub.build_ordered_collection(
                DB.followers,
                cursor=request.args.get('cursor'),
                map_func=lambda doc: doc['remote_actor'],
            )
        )                                                                      

    followers = [ACTOR_SERVICE.get(doc['remote_actor']) for doc in DB.followers.find(limit=50)]          
    return render_template(                         
        'followers.html',                           
        me=ME,
        notes=DB.inbox.find({'object.object.type': 'Note'}).count(),
        followers=DB.followers.count(),
        following=DB.following.count(),
        followers_data=followers,
    )


@app.route('/following')
def following():
    if is_api_request():
        return jsonify(
            **activitypub.build_ordered_collection(
                DB.following,
                cursor=request.args.get('cursor'),
                map_func=lambda doc: doc['remote_actor'],
            ),
        )
    
    following = [ACTOR_SERVICE.get(doc['remote_actor']) for doc in DB.following.find(limit=50)]
    return render_template(
        'following.html',
        me=ME,
        notes=DB.inbox.find({'object.object.type': 'Note'}).count(),
        followers=DB.followers.count(),
        following=DB.following.count(),
        following_data=following,
    )


@app.route('/tags/<tag>')
def tags(tag):
    if not DB.outbox.count({'activity.object.tag.type': 'Hashtag', 'activity.object.tag.name': '#'+tag}):
        abort(404)
    if not is_api_request():
        return render_template(
            'tags.html',
            tag=tag,
            outbox_data=DB.outbox.find({'type': 'Create', 'activity.object.type': 'Note', 'meta.deleted': False,
                                'activity.object.tag.type': 'Hashtag',
                                'activity.object.tag.name': '#'+tag}),
        )
    q = {
        'meta.deleted': False,
        'meta.undo': False,
        'type': ActivityTypes.CREATE.value,
        'activity.object.tag.type': 'Hashtag',
        'activity.object.tag.name': '#'+tag,
    }
    return jsonify(**activitypub.build_ordered_collection(
        DB.outbox,
        q=q,
        cursor=request.args.get('cursor'),
        map_func=lambda doc: doc['activity']['object']['id'],
        col_name=f'tags/{tag}',
    ))


@app.route('/liked')
def liked():
    if not is_api_request():
        abort(404)
    q = {
        'meta.deleted': False,
        'meta.undo': False,
        'type': ActivityTypes.LIKE.value,
    }
    return jsonify(**activitypub.build_ordered_collection(
        DB.outbox,
        q=q,
        cursor=request.args.get('cursor'),
        map_func=lambda doc: doc['activity']['object'],
        col_name='liked',
    ))

#######
# IndieAuth


def build_auth_resp(payload):
    if request.headers.get('Accept') == 'application/json':
        return Response(
            status=200,
            headers={'Content-Type': 'application/json'},
            response=json.dumps(payload),
        )
    return Response(
        status=200,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        response=urlencode(payload),
    )


def _get_prop(props, name, default=None):
    if name in props:
        items = props.get(name)
        if isinstance(items, list):
            return items[0]
        return items
    return default

def get_client_id_data(url):
    data = mf2py.parse(url=url)
    for item in data['items']:
        if 'h-x-app' in item['type'] or 'h-app' in item['type']:
            props = item.get('properties', {})
            print(props)
            return dict(
                logo=_get_prop(props, 'logo'),
                name=_get_prop(props, 'name'),
                url=_get_prop(props, 'url'),
            )
    return dict(
        logo=None,
        name=url,
        url=url,
    )


@app.route('/indieauth/flow', methods=['POST'])
@login_required                                     
def indieauth_flow():                               
    auth = dict(                                    
        scope=' '.join(request.form.getlist('scopes')),                                                  
        me=request.form.get('me'),                  
        client_id=request.form.get('client_id'),    
        state=request.form.get('state'),            
        redirect_uri=request.form.get('redirect_uri'),
        response_type=request.form.get('response_type'),
    )

    code = binascii.hexlify(os.urandom(8)).decode('utf-8')
    auth.update(
        code=code,
        verified=False,
    )
    print(auth)
    if not auth['redirect_uri']:
        abort(500)

    DB.indieauth.insert_one(auth)

    # FIXME(tsileo): fetch client ID and validate redirect_uri
    red = f'{auth["redirect_uri"]}?code={code}&state={auth["state"]}&me={auth["me"]}'
    return redirect(red)


@app.route('/indieauth', methods=['GET', 'POST'])   
def indieauth_endpoint():                           
    session['logged_in'] = True                     
    if request.method == 'GET':                     
        if not session.get('logged_in'):            
            return redirect(url_for('login', next=request.url))                                          

        me = request.args.get('me')                 
        # FIXME(tsileo): ensure me == ID            
        client_id = request.args.get('client_id')
        redirect_uri = request.args.get('redirect_uri')
        state = request.args.get('state', '')
        response_type = request.args.get('response_type', 'id')
        scope = request.args.get('scope', '').split()

        print('STATE', state)
        return render_template(
            'indieauth_flow.html',
            client=get_client_id_data(client_id),
            scopes=scope,
            redirect_uri=redirect_uri,
            state=state,
            response_type=response_type,
            client_id=client_id,
            me=me,
        )

    # Auth verification via POST
    code = request.form.get('code')
    redirect_uri = request.form.get('redirect_uri')
    client_id = request.form.get('client_id')

    auth = DB.indieauth.find_one_and_update(
        {'code': code, 'redirect_uri': redirect_uri, 'client_id': client_id},  #},  #  , 'verified': False},
        {'$set': {'verified': True}},
        sort=[('_id', pymongo.DESCENDING)],
    )
    print(auth)
    print(code, redirect_uri, client_id)

    if not auth:
        abort(403)
        return

    me = auth['me']
    state = auth['state']
    scope = ' '.join(auth['scope'])
    print('STATE', state)
    return build_auth_resp({'me': me, 'state': state, 'scope': scope})


@app.route('/token', methods=['GET', 'POST'])
def token_endpoint():
    if request.method == 'POST':
        code = request.form.get('code')
        me = request.form.get('me')
        redirect_uri = request.form.get('redirect_uri')
        client_id = request.form.get('client_id')

        auth = DB.indieauth.find_one({'code': code, 'me': me, 'redirect_uri': redirect_uri, 'client_id': client_id})
        if not auth:
            abort(403)
        scope = ' '.join(auth['scope'])
        payload = dict(me=me, client_id=client_id, scope=scope, ts=datetime.now().timestamp())
        token = JWT.dumps(payload).decode('utf-8')

        return build_auth_resp({'me': me, 'scope': scope, 'access_token': token})

    # Token verification
    token = request.headers.get('Authorization').replace('Bearer ', '')
    try:
        payload = JWT.loads(token)
    except BadSignature:
        abort(403)

    # TODO(tsileo): handle expiration

    return build_auth_resp({
        'me': payload['me'],
        'scope': payload['scope'],
        'client_id': payload['client_id'],
    })
