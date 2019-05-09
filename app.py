import binascii
import json
import logging
import mimetypes
import os
import traceback
import urllib
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from functools import wraps
from io import BytesIO
from typing import Any
from typing import Dict
from typing import Optional
from typing import Tuple
from urllib.parse import urlencode
from urllib.parse import urlparse

import bleach
import mf2py
import requests
import timeago
from bson.objectid import ObjectId
from dateutil import parser
from flask import Flask
from flask import Response
from flask import abort
from flask import jsonify as flask_jsonify
from flask import make_response
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from flask_wtf.csrf import CSRFProtect
from html2text import html2text
from itsdangerous import BadSignature
from little_boxes import activitypub as ap
from little_boxes.activitypub import ActivityType
from little_boxes.activitypub import _to_list
from little_boxes.activitypub import clean_activity
from little_boxes.activitypub import format_datetime
from little_boxes.activitypub import get_backend
from little_boxes.content_helper import parse_markdown
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import ActivityNotFoundError
from little_boxes.errors import BadActivityError
from little_boxes.errors import Error
from little_boxes.errors import NotAnActivityError
from little_boxes.errors import NotFromOutboxError
from little_boxes.httpsig import HTTPSigAuth
from little_boxes.httpsig import verify_request
from little_boxes.webfinger import get_actor_url
from little_boxes.webfinger import get_remote_follow_template
from passlib.hash import bcrypt
from requests.exceptions import HTTPError
from u2flib_server import u2f
from werkzeug.utils import secure_filename

import activitypub
import config
from activitypub import Box
from activitypub import _answer_key
from activitypub import embed_collection
from config import ADMIN_API_KEY
from config import BASE_URL
from config import DB
from config import DEBUG_MODE
from config import DOMAIN
from config import EMOJIS
from config import HEADERS
from config import ICON_URL
from config import ID
from config import JWT
from config import KEY
from config import ME
from config import MEDIA_CACHE
from config import PASS
from config import USER_AGENT
from config import USERNAME
from config import VERSION
from config import _drop_db
from poussetaches import PousseTaches
from utils import opengraph
from utils.key import get_secret_key
from utils.lookup import lookup
from utils.media import Kind

p = PousseTaches(
    os.getenv("MICROBLOGPUB_POUSSETACHES_HOST", "http://localhost:7991"),
    os.getenv("MICROBLOGPUB_INTERNAL_HOST", "http://localhost:5000"),
)

# p = PousseTaches("http://localhost:7991", "http://localhost:5000")

back = activitypub.MicroblogPubBackend()
ap.use_backend(back)

MY_PERSON = ap.Person(**ME)

app = Flask(__name__)
app.secret_key = get_secret_key("flask")
app.config.update(WTF_CSRF_CHECK_DEFAULT=False)
csrf = CSRFProtect(app)

logger = logging.getLogger(__name__)

# Hook up Flask logging with gunicorn
root_logger = logging.getLogger()
if os.getenv("FLASK_DEBUG"):
    logger.setLevel(logging.DEBUG)
    root_logger.setLevel(logging.DEBUG)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    root_logger.handlers = gunicorn_logger.handlers
    root_logger.setLevel(gunicorn_logger.level)

SIG_AUTH = HTTPSigAuth(KEY)


def verify_pass(pwd):
    return bcrypt.verify(pwd, PASS)


@app.context_processor
def inject_config():
    q = {
        "type": "Create",
        "activity.object.inReplyTo": None,
        "meta.deleted": False,
        "meta.public": True,
    }
    notes_count = DB.activities.find(
        {"box": Box.OUTBOX.value, "$or": [q, {"type": "Announce", "meta.undo": False}]}
    ).count()
    with_replies_count = DB.activities.find(
        {
            "box": Box.OUTBOX.value,
            "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
            "meta.undo": False,
            "meta.deleted": False,
            "meta.public": True,
        }
    ).count()
    liked_count = DB.activities.count(
        {
            "box": Box.OUTBOX.value,
            "meta.deleted": False,
            "meta.undo": False,
            "type": ActivityType.LIKE.value,
        }
    )
    followers_q = {
        "box": Box.INBOX.value,
        "type": ActivityType.FOLLOW.value,
        "meta.undo": False,
    }
    following_q = {
        "box": Box.OUTBOX.value,
        "type": ActivityType.FOLLOW.value,
        "meta.undo": False,
    }

    return dict(
        microblogpub_version=VERSION,
        config=config,
        logged_in=session.get("logged_in", False),
        followers_count=DB.activities.count(followers_q),
        following_count=DB.activities.count(following_q),
        notes_count=notes_count,
        liked_count=liked_count,
        with_replies_count=with_replies_count,
        me=ME,
        base_url=config.BASE_URL,
    )


@app.after_request
def set_x_powered_by(response):
    response.headers["X-Powered-By"] = "microblog.pub"
    return response


# HTML/templates helper
ALLOWED_TAGS = [
    "a",
    "abbr",
    "acronym",
    "b",
    "br",
    "blockquote",
    "code",
    "pre",
    "em",
    "i",
    "li",
    "ol",
    "strong",
    "ul",
    "span",
    "div",
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
]


def clean_html(html):
    try:
        return bleach.clean(html, tags=ALLOWED_TAGS)
    except Exception:
        return ""


_GRIDFS_CACHE: Dict[Tuple[Kind, str, Optional[int]], str] = {}


def _get_file_url(url, size, kind):
    k = (kind, url, size)
    cached = _GRIDFS_CACHE.get(k)
    if cached:
        return cached

    doc = MEDIA_CACHE.get_file(url, size, kind)
    if doc:
        u = f"/media/{str(doc._id)}"
        _GRIDFS_CACHE[k] = u
        return u

    # MEDIA_CACHE.cache(url, kind)
    app.logger.error(f"cache not available for {url}/{size}/{kind}")
    return url


@app.template_filter()
def gtone(n):
    return n > 1


@app.template_filter()
def gtnow(dtstr):
    return format_datetime(datetime.now().astimezone()) > dtstr


@app.template_filter()
def remove_mongo_id(dat):
    if isinstance(dat, list):
        return [remove_mongo_id(item) for item in dat]
    if "_id" in dat:
        dat["_id"] = str(dat["_id"])
    for k, v in dat.items():
        if isinstance(v, dict):
            dat[k] = remove_mongo_id(dat[k])
    return dat


@app.template_filter()
def get_video_link(data):
    for link in data:
        if link.get("mimeType", "").startswith("video/"):
            return link.get("href")
    return None


@app.template_filter()
def get_actor_icon_url(url, size):
    return _get_file_url(url, size, Kind.ACTOR_ICON)


@app.template_filter()
def get_attachment_url(url, size):
    return _get_file_url(url, size, Kind.ATTACHMENT)


@app.template_filter()
def get_og_image_url(url, size=100):
    try:
        return _get_file_url(url, size, Kind.OG_IMAGE)
    except Exception:
        return ""


@app.template_filter()
def permalink_id(val):
    return str(hash(val))


@app.template_filter()
def quote_plus(t):
    return urllib.parse.quote_plus(t)


@app.template_filter()
def is_from_outbox(t):
    return t.startswith(ID)


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
def url_or_id(d):
    if isinstance(d, dict):
        if "url" in d:
            return d["url"]
        else:
            return d["id"]
    return ""


@app.template_filter()
def get_url(u):
    print(f"GET_URL({u!r})")
    if isinstance(u, list):
        for l in u:
            if l.get("mimeType") == "text/html":
                u = l
    if isinstance(u, dict):
        return u["href"]
    elif isinstance(u, str):
        return u
    else:
        return u


@app.template_filter()
def get_actor(url):
    if not url:
        return None
    if isinstance(url, list):
        url = url[0]
    if isinstance(url, dict):
        url = url.get("id")
    print(f"GET_ACTOR {url}")
    try:
        return get_backend().fetch_iri(url)
    except (ActivityNotFoundError, ActivityGoneError):
        return f"Deleted<{url}>"
    except Exception as exc:
        return f"Error<{url}/{exc!r}>"


@app.template_filter()
def format_time(val):
    if val:
        dt = parser.parse(val)
        return datetime.strftime(dt, "%B %d, %Y, %H:%M %p")
    return val


@app.template_filter()
def format_ts(val):
    return datetime.fromtimestamp(val).strftime("%B %d, %Y, %H:%M %p")


@app.template_filter()
def gt_ts(val):
    return datetime.now() > datetime.fromtimestamp(val)


@app.template_filter()
def format_timeago(val):
    if val:
        dt = parser.parse(val)
        return timeago.format(dt, datetime.now(timezone.utc))
    return val


@app.template_filter()
def has_type(doc, _types):
    for _type in _to_list(_types):
        if _type in _to_list(doc["type"]):
            return True
    return False


@app.template_filter()
def has_actor_type(doc):
    for t in ap.ACTOR_TYPES:
        if has_type(doc, t.value):
            return True
    return False


def _is_img(filename):
    filename = filename.lower()
    if (
        filename.endswith(".png")
        or filename.endswith(".jpg")
        or filename.endswith(".jpeg")
        or filename.endswith(".gif")
        or filename.endswith(".svg")
    ):
        return True
    return False


@app.template_filter()
def not_only_imgs(attachment):
    for a in attachment:
        if isinstance(a, dict) and not _is_img(a["url"]):
            return True
        if isinstance(a, str) and not _is_img(a):
            return True
    return False


@app.template_filter()
def is_img(filename):
    return _is_img(filename)


@app.template_filter()
def get_answer_count(choice, meta):
    print(choice, meta)
    return meta.get("question_answers", {}).get(_answer_key(choice), 0)


def add_response_headers(headers={}):
    """This decorator adds the headers passed in to the response"""

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            resp = make_response(f(*args, **kwargs))
            h = resp.headers
            for header, value in headers.items():
                h[header] = value
            return resp

        return decorated_function

    return decorator


def noindex(f):
    """This decorator passes X-Robots-Tag: noindex, nofollow"""
    return add_response_headers({"X-Robots-Tag": "noindex, nofollow"})(f)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("admin_login", next=request.url))
        return f(*args, **kwargs)

    return decorated_function


def _api_required():
    if session.get("logged_in"):
        if request.method not in ["GET", "HEAD"]:
            # If a standard API request is made with a "login session", it must havw a CSRF token
            csrf.protect()
        return

    # Token verification
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        # IndieAuth token
        token = request.form.get("access_token", "")

    # Will raise a BadSignature on bad auth
    payload = JWT.loads(token)
    logger.info(f"api call by {payload}")


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
    if "@context" not in data:
        data["@context"] = config.DEFAULT_CTX
    return Response(
        response=json.dumps(data),
        headers={
            "Content-Type": "application/json"
            if app.debug
            else "application/activity+json"
        },
    )


def is_api_request():
    h = request.headers.get("Accept")
    if h is None:
        return False
    h = h.split(",")[0]
    if h in HEADERS or h == "application/json":
        return True
    return False


@app.errorhandler(ValueError)
def handle_value_error(error):
    logger.error(
        f"caught value error: {error!r}, {traceback.format_tb(error.__traceback__)}"
    )
    response = flask_jsonify(message=error.args[0])
    response.status_code = 400
    return response


@app.errorhandler(Error)
def handle_activitypub_error(error):
    logger.error(
        f"caught activitypub error {error!r}, {traceback.format_tb(error.__traceback__)}"
    )
    response = flask_jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


class TaskError(Exception):
    """Raised to log the error for poussetaches."""

    def __init__(self):
        self.message = traceback.format_exc()


@app.errorhandler(TaskError)
def handle_task_error(error):
    logger.error(
        f"caught activitypub error {error!r}, {traceback.format_tb(error.__traceback__)}"
    )
    response = flask_jsonify({"traceback": error.message})
    response.status_code = 500
    return response


# @app.errorhandler(Exception)
# def handle_other_error(error):
#    logger.error(
#        f"caught error {error!r}, {traceback.format_tb(error.__traceback__)}"
#    )
#    response = flask_jsonify({})
#    response.status_code = 500
#    return response


# App routes

ROBOTS_TXT = """User-agent: *
Disallow: /login
Disallow: /admin/
Disallow: /static/
Disallow: /media/
Disallow: /uploads/"""


@app.route("/robots.txt")
def robots_txt():
    return Response(response=ROBOTS_TXT, headers={"Content-Type": "text/plain"})


@app.route("/media/<media_id>")
@noindex
def serve_media(media_id):
    f = MEDIA_CACHE.fs.get(ObjectId(media_id))
    resp = app.response_class(f, direct_passthrough=True, mimetype=f.content_type)
    resp.headers.set("Content-Length", f.length)
    resp.headers.set("ETag", f.md5)
    resp.headers.set(
        "Last-Modified", f.uploadDate.strftime("%a, %d %b %Y %H:%M:%S GMT")
    )
    resp.headers.set("Cache-Control", "public,max-age=31536000,immutable")
    resp.headers.set("Content-Encoding", "gzip")
    return resp


@app.route("/uploads/<oid>/<fname>")
def serve_uploads(oid, fname):
    f = MEDIA_CACHE.fs.get(ObjectId(oid))
    resp = app.response_class(f, direct_passthrough=True, mimetype=f.content_type)
    resp.headers.set("Content-Length", f.length)
    resp.headers.set("ETag", f.md5)
    resp.headers.set(
        "Last-Modified", f.uploadDate.strftime("%a, %d %b %Y %H:%M:%S GMT")
    )
    resp.headers.set("Cache-Control", "public,max-age=31536000,immutable")
    resp.headers.set("Content-Encoding", "gzip")
    return resp


#######
# Login


@app.route("/admin/logout")
@login_required
def admin_logout():
    session["logged_in"] = False
    return redirect("/")


@app.route("/login", methods=["POST", "GET"])
@noindex
def admin_login():
    if session.get("logged_in") is True:
        return redirect(url_for("admin_notifications"))

    devices = [doc["device"] for doc in DB.u2f.find()]
    u2f_enabled = True if devices else False
    if request.method == "POST":
        csrf.protect()
        # 1. Check regular password login flow
        pwd = request.form.get("pass")
        if pwd:
            if verify_pass(pwd):
                session["logged_in"] = True
                return redirect(
                    request.args.get("redirect") or url_for("admin_notifications")
                )
            else:
                abort(403)
        # 2. Check for U2F payload, if any
        elif devices:
            resp = json.loads(request.form.get("resp"))
            try:
                u2f.complete_authentication(session["challenge"], resp)
            except ValueError as exc:
                print("failed", exc)
                abort(403)
                return
            finally:
                session["challenge"] = None

            session["logged_in"] = True
            return redirect(
                request.args.get("redirect") or url_for("admin_notifications")
            )
        else:
            abort(401)

    payload = None
    if devices:
        payload = u2f.begin_authentication(ID, devices)
        session["challenge"] = payload

    return render_template("login.html", u2f_enabled=u2f_enabled, payload=payload)


@app.route("/remote_follow", methods=["GET", "POST"])
def remote_follow():
    if request.method == "GET":
        return render_template("remote_follow.html")

    csrf.protect()
    profile = request.form.get("profile")
    if not profile.startswith("@"):
        profile = f"@{profile}"
    return redirect(
        get_remote_follow_template(profile).format(uri=f"{USERNAME}@{DOMAIN}")
    )


@app.route("/authorize_follow", methods=["GET", "POST"])
@login_required
def authorize_follow():
    if request.method == "GET":
        return render_template(
            "authorize_remote_follow.html", profile=request.args.get("profile")
        )

    actor = get_actor_url(request.form.get("profile"))
    if not actor:
        abort(500)

    q = {
        "box": Box.OUTBOX.value,
        "type": ActivityType.FOLLOW.value,
        "meta.undo": False,
        "activity.object": actor,
    }
    if DB.activities.count(q) > 0:
        return redirect("/following")

    follow = ap.Follow(actor=MY_PERSON.id, object=actor)
    post_to_outbox(follow)

    return redirect("/following")


@app.route("/u2f/register", methods=["GET", "POST"])
@login_required
def u2f_register():
    # TODO(tsileo): ensure no duplicates
    if request.method == "GET":
        payload = u2f.begin_registration(ID)
        session["challenge"] = payload
        return render_template("u2f.html", payload=payload)
    else:
        resp = json.loads(request.form.get("resp"))
        device, device_cert = u2f.complete_registration(session["challenge"], resp)
        session["challenge"] = None
        DB.u2f.insert_one({"device": device, "cert": device_cert})
        session["logged_in"] = False
        return redirect("/login")


#######
# Activity pub routes
@app.route("/drop_cache")
@login_required
def drop_cache():
    DB.actors.drop()
    return "Done"


def paginated_query(db, q, limit=25, sort_key="_id"):
    older_than = newer_than = None
    query_sort = -1
    first_page = not request.args.get("older_than") and not request.args.get(
        "newer_than"
    )

    query_older_than = request.args.get("older_than")
    query_newer_than = request.args.get("newer_than")

    if query_older_than:
        q["_id"] = {"$lt": ObjectId(query_older_than)}
    elif query_newer_than:
        q["_id"] = {"$gt": ObjectId(query_newer_than)}
        query_sort = 1

    outbox_data = list(db.find(q, limit=limit + 1).sort(sort_key, query_sort))
    outbox_len = len(outbox_data)
    outbox_data = sorted(
        outbox_data[:limit], key=lambda x: str(x[sort_key]), reverse=True
    )

    if query_older_than:
        newer_than = str(outbox_data[0]["_id"])
        if outbox_len == limit + 1:
            older_than = str(outbox_data[-1]["_id"])
    elif query_newer_than:
        older_than = str(outbox_data[-1]["_id"])
        if outbox_len == limit + 1:
            newer_than = str(outbox_data[0]["_id"])
    elif first_page and outbox_len == limit + 1:
        older_than = str(outbox_data[-1]["_id"])

    return outbox_data, older_than, newer_than


CACHING = True


def _get_cached(type_="html", arg=None):
    if not CACHING:
        return None
    logged_in = session.get("logged_in")
    if not logged_in:
        cached = DB.cache2.find_one({"path": request.path, "type": type_, "arg": arg})
        if cached:
            app.logger.info("from cache")
            return cached["response_data"]
    return None


def _cache(resp, type_="html", arg=None):
    if not CACHING:
        return None
    logged_in = session.get("logged_in")
    if not logged_in:
        DB.cache2.update_one(
            {"path": request.path, "type": type_, "arg": arg},
            {"$set": {"response_data": resp, "date": datetime.now(timezone.utc)}},
            upsert=True,
        )
    return None


@app.route("/")
def index():
    if is_api_request():
        return jsonify(**ME)
    cache_arg = (
        f"{request.args.get('older_than', '')}:{request.args.get('newer_than', '')}"
    )
    cached = _get_cached("html", cache_arg)
    if cached:
        return cached

    q = {
        "box": Box.OUTBOX.value,
        "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
        "activity.object.inReplyTo": None,
        "meta.deleted": False,
        "meta.undo": False,
        "$or": [{"meta.pinned": False}, {"meta.pinned": {"$exists": False}}],
    }
    print(list(DB.activities.find(q)))

    pinned = []
    # Only fetch the pinned notes if we're on the first page
    if not request.args.get("older_than") and not request.args.get("newer_than"):
        q_pinned = {
            "box": Box.OUTBOX.value,
            "type": ActivityType.CREATE.value,
            "meta.deleted": False,
            "meta.undo": False,
            "meta.pinned": True,
        }
        pinned = list(DB.activities.find(q_pinned))

    outbox_data, older_than, newer_than = paginated_query(
        DB.activities, q, limit=25 - len(pinned)
    )

    resp = render_template(
        "index.html",
        outbox_data=outbox_data,
        older_than=older_than,
        newer_than=newer_than,
        pinned=pinned,
    )
    _cache(resp, "html", cache_arg)
    return resp


@app.route("/with_replies")
@login_required
def with_replies():
    q = {
        "box": Box.OUTBOX.value,
        "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
        "meta.deleted": False,
        "meta.public": True,
        "meta.undo": False,
    }
    outbox_data, older_than, newer_than = paginated_query(DB.activities, q)

    return render_template(
        "index.html",
        outbox_data=outbox_data,
        older_than=older_than,
        newer_than=newer_than,
    )


def _build_thread(data, include_children=True):  # noqa: C901
    data["_requested"] = True
    app.logger.info(f"_build_thread({data!r})")
    root_id = data["meta"].get("thread_root_parent", data["activity"]["object"]["id"])

    query = {
        "$or": [{"meta.thread_root_parent": root_id}, {"activity.object.id": root_id}],
        "meta.deleted": False,
    }
    replies = [data]
    for dat in DB.activities.find(query):
        if dat["type"][0] == ActivityType.CREATE.value:
            replies.append(dat)
        else:
            # Make a Note/Question/... looks like a Create
            dat = {
                "activity": {"object": dat["activity"]},
                "meta": dat["meta"],
                "_id": dat["_id"],
            }
            replies.append(dat)

    replies = sorted(replies, key=lambda d: d["activity"]["object"]["published"])

    # Index all the IDs in order to build a tree
    idx = {}
    replies2 = []
    for rep in replies:
        rep_id = rep["activity"]["object"]["id"]
        if rep_id in idx:
            continue
        idx[rep_id] = rep.copy()
        idx[rep_id]["_nodes"] = []
        replies2.append(rep)

    # Build the tree
    for rep in replies2:
        rep_id = rep["activity"]["object"]["id"]
        if rep_id == root_id:
            continue
        reply_of = ap._get_id(rep["activity"]["object"]["inReplyTo"])
        try:
            idx[reply_of]["_nodes"].append(rep)
        except KeyError:
            app.logger.info(f"{reply_of} is not there! skipping {rep}")

    # Flatten the tree
    thread = []

    def _flatten(node, level=0):
        node["_level"] = level
        thread.append(node)

        for snode in sorted(
            idx[node["activity"]["object"]["id"]]["_nodes"],
            key=lambda d: d["activity"]["object"]["published"],
        ):
            _flatten(snode, level=level + 1)

    try:
        _flatten(idx[root_id])
    except KeyError:
        app.logger.info(f"{root_id} is not there! skipping")

    return thread


@app.route("/note/<note_id>")
def note_by_id(note_id):
    if is_api_request():
        return redirect(url_for("outbox_activity", item_id=note_id))

    data = DB.activities.find_one(
        {"box": Box.OUTBOX.value, "remote_id": back.activity_url(note_id)}
    )
    if not data:
        abort(404)
    if data["meta"].get("deleted", False):
        abort(410)

    thread = _build_thread(data)
    app.logger.info(f"thread={thread!r}")

    raw_likes = list(
        DB.activities.find(
            {
                "meta.undo": False,
                "meta.deleted": False,
                "type": ActivityType.LIKE.value,
                "$or": [
                    # FIXME(tsileo): remove all the useless $or
                    {"activity.object.id": data["activity"]["object"]["id"]},
                    {"activity.object": data["activity"]["object"]["id"]},
                ],
            }
        )
    )
    likes = []
    for doc in raw_likes:
        try:
            likes.append(doc["meta"]["actor"])
        except Exception:
            app.logger.exception(f"invalid doc: {doc!r}")
    app.logger.info(f"likes={likes!r}")

    raw_shares = list(
        DB.activities.find(
            {
                "meta.undo": False,
                "meta.deleted": False,
                "type": ActivityType.ANNOUNCE.value,
                "$or": [
                    {"activity.object.id": data["activity"]["object"]["id"]},
                    {"activity.object": data["activity"]["object"]["id"]},
                ],
            }
        )
    )
    shares = []
    for doc in raw_shares:
        try:
            shares.append(doc["meta"]["actor"])
        except Exception:
            app.logger.exception(f"invalid doc: {doc!r}")
    app.logger.info(f"shares={shares!r}")

    return render_template(
        "note.html", likes=likes, shares=shares, thread=thread, note=data
    )


@app.route("/nodeinfo")
def nodeinfo():
    response = _get_cached("api")
    cached = True
    if not response:
        cached = False
        q = {
            "box": Box.OUTBOX.value,
            "meta.deleted": False,  # TODO(tsileo): retrieve deleted and expose tombstone
            "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
        }

        response = json.dumps(
            {
                "version": "2.0",
                "software": {
                    "name": "microblogpub",
                    "version": f"Microblog.pub {VERSION}",
                },
                "protocols": ["activitypub"],
                "services": {"inbound": [], "outbound": []},
                "openRegistrations": False,
                "usage": {"users": {"total": 1}, "localPosts": DB.activities.count(q)},
                "metadata": {
                    "sourceCode": "https://github.com/tsileo/microblog.pub",
                    "nodeName": f"@{USERNAME}@{DOMAIN}",
                },
            }
        )

    if not cached:
        _cache(response, "api")
    return Response(
        headers={
            "Content-Type": "application/json; profile=http://nodeinfo.diaspora.software/ns/schema/2.0#"
        },
        response=response,
    )


@app.route("/.well-known/nodeinfo")
def wellknown_nodeinfo():
    return flask_jsonify(
        links=[
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                "href": f"{ID}/nodeinfo",
            }
        ]
    )


@app.route("/.well-known/webfinger")
def wellknown_webfinger():
    """Enable WebFinger support, required for Mastodon interopability."""
    # TODO(tsileo): move this to little-boxes?
    resource = request.args.get("resource")
    if resource not in [f"acct:{USERNAME}@{DOMAIN}", ID]:
        abort(404)

    out = {
        "subject": f"acct:{USERNAME}@{DOMAIN}",
        "aliases": [ID],
        "links": [
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": BASE_URL,
            },
            {"rel": "self", "type": "application/activity+json", "href": ID},
            {
                "rel": "http://ostatus.org/schema/1.0/subscribe",
                "template": BASE_URL + "/authorize_follow?profile={uri}",
            },
            {"rel": "magic-public-key", "href": KEY.to_magic_key()},
            {
                "href": ICON_URL,
                "rel": "http://webfinger.net/rel/avatar",
                "type": mimetypes.guess_type(ICON_URL)[0],
            },
        ],
    }

    return Response(
        response=json.dumps(out),
        headers={
            "Content-Type": "application/jrd+json; charset=utf-8"
            if not app.debug
            else "application/json"
        },
    )


def add_extra_collection(raw_doc: Dict[str, Any]) -> Dict[str, Any]:
    if raw_doc["activity"]["type"] != ActivityType.CREATE.value:
        return raw_doc

    raw_doc["activity"]["object"]["replies"] = embed_collection(
        raw_doc.get("meta", {}).get("count_direct_reply", 0),
        f'{raw_doc["remote_id"]}/replies',
    )

    raw_doc["activity"]["object"]["likes"] = embed_collection(
        raw_doc.get("meta", {}).get("count_like", 0), f'{raw_doc["remote_id"]}/likes'
    )

    raw_doc["activity"]["object"]["shares"] = embed_collection(
        raw_doc.get("meta", {}).get("count_boost", 0), f'{raw_doc["remote_id"]}/shares'
    )

    return raw_doc


def remove_context(activity: Dict[str, Any]) -> Dict[str, Any]:
    if "@context" in activity:
        del activity["@context"]
    return activity


def _add_answers_to_questions(raw_doc: Dict[str, Any]) -> None:
    activity = raw_doc["activity"]
    if (
        ap._has_type(activity["type"], ActivityType.CREATE)
        and "object" in activity
        and ap._has_type(activity["object"]["type"], ActivityType.QUESTION)
    ):
        for choice in activity["object"].get("oneOf", activity["object"].get("anyOf")):
            choice["replies"] = {
                "type": ActivityType.COLLECTION.value,
                "totalItems": raw_doc["meta"]
                .get("question_answers", {})
                .get(_answer_key(choice["name"]), 0),
            }
        now = datetime.now().astimezone()
        if format_datetime(now) > activity["object"]["endTime"]:
            activity["object"]["closed"] = activity["object"]["endTime"]


def activity_from_doc(raw_doc: Dict[str, Any], embed: bool = False) -> Dict[str, Any]:
    raw_doc = add_extra_collection(raw_doc)
    activity = clean_activity(raw_doc["activity"])

    # Handle Questions
    # TODO(tsileo): what about object embedded by ID/URL?
    _add_answers_to_questions(raw_doc)
    if embed:
        return remove_context(activity)
    return activity


@app.route("/outbox", methods=["GET", "POST"])
def outbox():
    if request.method == "GET":
        if not is_api_request():
            abort(404)
        # TODO(tsileo): returns the whole outbox if authenticated
        q = {
            "box": Box.OUTBOX.value,
            "meta.deleted": False,
            "meta.undo": False,
            "meta.public": True,
            "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
        }
        return jsonify(
            **activitypub.build_ordered_collection(
                DB.activities,
                q=q,
                cursor=request.args.get("cursor"),
                map_func=lambda doc: activity_from_doc(doc, embed=True),
                col_name="outbox",
            )
        )

    # Handle POST request
    try:
        _api_required()
    except BadSignature:
        abort(401)

    data = request.get_json(force=True)
    print(data)
    activity = ap.parse_activity(data)
    activity_id = post_to_outbox(activity)

    return Response(status=201, headers={"Location": activity_id})


@app.route("/outbox/<item_id>")
def outbox_detail(item_id):
    doc = DB.activities.find_one(
        {"box": Box.OUTBOX.value, "remote_id": back.activity_url(item_id)}
    )
    if not doc:
        abort(404)

    if doc["meta"].get("deleted", False):
        obj = ap.parse_activity(doc["activity"])
        resp = jsonify(**obj.get_tombstone().to_dict())
        resp.status_code = 410
        return resp
    return jsonify(**activity_from_doc(doc))


@app.route("/outbox/<item_id>/activity")
def outbox_activity(item_id):
    data = DB.activities.find_one(
        {"box": Box.OUTBOX.value, "remote_id": back.activity_url(item_id)}
    )
    if not data:
        abort(404)

    obj = activity_from_doc(data)
    if data["meta"].get("deleted", False):
        obj = ap.parse_activity(data["activity"])
        resp = jsonify(**obj.get_object().get_tombstone().to_dict())
        resp.status_code = 410
        return resp

    if obj["type"] != ActivityType.CREATE.value:
        abort(404)
    return jsonify(**obj["object"])


@app.route("/outbox/<item_id>/replies")
def outbox_activity_replies(item_id):
    if not is_api_request():
        abort(404)
    data = DB.activities.find_one(
        {
            "box": Box.OUTBOX.value,
            "remote_id": back.activity_url(item_id),
            "meta.deleted": False,
        }
    )
    if not data:
        abort(404)
    obj = ap.parse_activity(data["activity"])
    if obj.ACTIVITY_TYPE != ActivityType.CREATE:
        abort(404)

    q = {
        "meta.deleted": False,
        "type": ActivityType.CREATE.value,
        "activity.object.inReplyTo": obj.get_object().id,
    }

    return jsonify(
        **activitypub.build_ordered_collection(
            DB.activities,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: doc["activity"]["object"],
            col_name=f"outbox/{item_id}/replies",
            first_page=request.args.get("page") == "first",
        )
    )


@app.route("/outbox/<item_id>/likes")
def outbox_activity_likes(item_id):
    if not is_api_request():
        abort(404)
    data = DB.activities.find_one(
        {
            "box": Box.OUTBOX.value,
            "remote_id": back.activity_url(item_id),
            "meta.deleted": False,
        }
    )
    if not data:
        abort(404)
    obj = ap.parse_activity(data["activity"])
    if obj.ACTIVITY_TYPE != ActivityType.CREATE:
        abort(404)

    q = {
        "meta.undo": False,
        "type": ActivityType.LIKE.value,
        "$or": [
            {"activity.object.id": obj.get_object().id},
            {"activity.object": obj.get_object().id},
        ],
    }

    return jsonify(
        **activitypub.build_ordered_collection(
            DB.activities,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: remove_context(doc["activity"]),
            col_name=f"outbox/{item_id}/likes",
            first_page=request.args.get("page") == "first",
        )
    )


@app.route("/outbox/<item_id>/shares")
def outbox_activity_shares(item_id):
    if not is_api_request():
        abort(404)
    data = DB.activities.find_one(
        {
            "box": Box.OUTBOX.value,
            "remote_id": back.activity_url(item_id),
            "meta.deleted": False,
        }
    )
    if not data:
        abort(404)
    obj = ap.parse_activity(data["activity"])
    if obj.ACTIVITY_TYPE != ActivityType.CREATE:
        abort(404)

    q = {
        "meta.undo": False,
        "type": ActivityType.ANNOUNCE.value,
        "$or": [
            {"activity.object.id": obj.get_object().id},
            {"activity.object": obj.get_object().id},
        ],
    }

    return jsonify(
        **activitypub.build_ordered_collection(
            DB.activities,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: remove_context(doc["activity"]),
            col_name=f"outbox/{item_id}/shares",
            first_page=request.args.get("page") == "first",
        )
    )


@app.route("/admin", methods=["GET"])
@login_required
def admin():
    q = {
        "meta.deleted": False,
        "meta.undo": False,
        "type": ActivityType.LIKE.value,
        "box": Box.OUTBOX.value,
    }
    col_liked = DB.activities.count(q)

    return render_template(
        "admin.html",
        instances=list(DB.instances.find()),
        inbox_size=DB.activities.count({"box": Box.INBOX.value}),
        outbox_size=DB.activities.count({"box": Box.OUTBOX.value}),
        col_liked=col_liked,
        col_followers=DB.activities.count(
            {
                "box": Box.INBOX.value,
                "type": ActivityType.FOLLOW.value,
                "meta.undo": False,
            }
        ),
        col_following=DB.activities.count(
            {
                "box": Box.OUTBOX.value,
                "type": ActivityType.FOLLOW.value,
                "meta.undo": False,
            }
        ),
    )


@app.route("/admin/indieauth", methods=["GET"])
@login_required
def admin_indieauth():
    return render_template(
        "admin_indieauth.html",
        indieauth_actions=DB.indieauth.find().sort("ts", -1).limit(100),
    )


@app.route("/admin/tasks", methods=["GET"])
@login_required
def admin_tasks():
    return render_template(
        "admin_tasks.html",
        success=p.get_success(),
        dead=p.get_dead(),
        waiting=p.get_waiting(),
        cron=p.get_cron(),
    )


@app.route("/admin/lookup", methods=["GET", "POST"])
@login_required
def admin_lookup():
    data = None
    meta = None
    if request.method == "POST":
        if request.form.get("url"):
            data = lookup(request.form.get("url"))
            if data.has_type(ActivityType.ANNOUNCE):
                meta = dict(
                    object=data.get_object().to_dict(),
                    object_actor=data.get_object().get_actor().to_dict(),
                    actor=data.get_actor().to_dict(),
                )

        print(data)
        app.logger.debug(data.to_dict())
    return render_template(
        "lookup.html", data=data, meta=meta, url=request.form.get("url")
    )


@app.route("/admin/thread")
@login_required
def admin_thread():
    data = DB.activities.find_one(
        {
            "type": ActivityType.CREATE.value,
            "activity.object.id": request.args.get("oid"),
        }
    )

    if not data:
        abort(404)
    if data["meta"].get("deleted", False):
        abort(410)
    thread = _build_thread(data)

    tpl = "note.html"
    if request.args.get("debug"):
        tpl = "note_debug.html"
    return render_template(tpl, thread=thread, note=data)


@app.route("/admin/new", methods=["GET"])
@login_required
def admin_new():
    reply_id = None
    content = ""
    thread = []
    print(request.args)
    if request.args.get("reply"):
        data = DB.activities.find_one({"activity.object.id": request.args.get("reply")})
        if data:
            reply = ap.parse_activity(data["activity"])
        else:
            data = dict(
                meta={},
                activity=dict(
                    object=get_backend().fetch_iri(request.args.get("reply"))
                ),
            )
            reply = ap.parse_activity(data["activity"]["object"])

        reply_id = reply.id
        if reply.ACTIVITY_TYPE == ActivityType.CREATE:
            reply_id = reply.get_object().id
        actor = reply.get_actor()
        domain = urlparse(actor.id).netloc
        # FIXME(tsileo): if reply of reply, fetch all participants
        content = f"@{actor.preferredUsername}@{domain} "
        thread = _build_thread(data)

    return render_template(
        "new.html",
        reply=reply_id,
        content=content,
        thread=thread,
        emojis=EMOJIS.split(" "),
    )


@app.route("/admin/notifications")
@login_required
def admin_notifications():
    # Setup the cron for deleting old activities

    # FIXME(tsileo): put back to 12h
    p.push({}, "/task/cleanup", schedule="@every 1h")

    # Trigger a cleanup if asked
    if request.args.get("cleanup"):
        p.push({}, "/task/cleanup")

    # FIXME(tsileo): show unfollow (performed by the current actor) and liked???
    mentions_query = {
        "type": ActivityType.CREATE.value,
        "activity.object.tag.type": "Mention",
        "activity.object.tag.name": f"@{USERNAME}@{DOMAIN}",
        "meta.deleted": False,
    }
    replies_query = {
        "type": ActivityType.CREATE.value,
        "activity.object.inReplyTo": {"$regex": f"^{BASE_URL}"},
    }
    announced_query = {
        "type": ActivityType.ANNOUNCE.value,
        "activity.object": {"$regex": f"^{BASE_URL}"},
    }
    new_followers_query = {"type": ActivityType.FOLLOW.value}
    unfollow_query = {
        "type": ActivityType.UNDO.value,
        "activity.object.type": ActivityType.FOLLOW.value,
    }
    likes_query = {
        "type": ActivityType.LIKE.value,
        "activity.object": {"$regex": f"^{BASE_URL}"},
    }
    followed_query = {"type": ActivityType.ACCEPT.value}
    q = {
        "box": Box.INBOX.value,
        "$or": [
            mentions_query,
            announced_query,
            replies_query,
            new_followers_query,
            followed_query,
            unfollow_query,
            likes_query,
        ],
    }
    inbox_data, older_than, newer_than = paginated_query(DB.activities, q)

    return render_template(
        "stream.html",
        inbox_data=inbox_data,
        older_than=older_than,
        newer_than=newer_than,
    )


@app.route("/api/key")
@login_required
def api_user_key():
    return flask_jsonify(api_key=ADMIN_API_KEY)


def _user_api_arg(key: str, **kwargs):
    """Try to get the given key from the requests, try JSON body, form data and query arg."""
    if request.is_json:
        oid = request.json.get(key)
    else:
        oid = request.args.get(key) or request.form.get(key)

    if not oid:
        if "default" in kwargs:
            app.logger.info(f'{key}={kwargs.get("default")}')
            return kwargs.get("default")

        raise ValueError(f"missing {key}")

    app.logger.info(f"{key}={oid}")
    return oid


def _user_api_get_note(from_outbox: bool = False):
    oid = _user_api_arg("id")
    app.logger.info(f"fetching {oid}")
    note = ap.parse_activity(get_backend().fetch_iri(oid))
    if from_outbox and not note.id.startswith(ID):
        raise NotFromOutboxError(
            f"cannot load {note.id}, id must be owned by the server"
        )

    return note


def _user_api_response(**kwargs):
    _redirect = _user_api_arg("redirect", default=None)
    if _redirect:
        return redirect(_redirect)

    resp = flask_jsonify(**kwargs)
    resp.status_code = 201
    return resp


@app.route("/api/note/delete", methods=["POST"])
@api_required
def api_delete():
    """API endpoint to delete a Note activity."""
    note = _user_api_get_note(from_outbox=True)

    delete = ap.Delete(actor=ID, object=ap.Tombstone(id=note.id).to_dict(embed=True))

    delete_id = post_to_outbox(delete)

    return _user_api_response(activity=delete_id)


@app.route("/api/boost", methods=["POST"])
@api_required
def api_boost():
    note = _user_api_get_note()

    announce = note.build_announce(MY_PERSON)
    announce_id = post_to_outbox(announce)

    return _user_api_response(activity=announce_id)


@app.route("/api/vote", methods=["POST"])
@api_required
def api_vote():
    oid = _user_api_arg("id")
    app.logger.info(f"fetching {oid}")
    note = ap.parse_activity(get_backend().fetch_iri(oid))
    choice = _user_api_arg("choice")

    raw_note = dict(
        attributedTo=MY_PERSON.id,
        cc=[],
        to=note.get_actor().id,
        name=choice,
        tag=[],
        inReplyTo=note.id,
    )

    note = ap.Note(**raw_note)
    create = note.build_create()
    create_id = post_to_outbox(create)

    return _user_api_response(activity=create_id)


@app.route("/api/like", methods=["POST"])
@api_required
def api_like():
    note = _user_api_get_note()

    like = note.build_like(MY_PERSON)
    like_id = post_to_outbox(like)

    return _user_api_response(activity=like_id)


@app.route("/api/note/pin", methods=["POST"])
@api_required
def api_pin():
    note = _user_api_get_note(from_outbox=True)

    DB.activities.update_one(
        {"activity.object.id": note.id, "box": Box.OUTBOX.value},
        {"$set": {"meta.pinned": True}},
    )

    return _user_api_response(pinned=True)


@app.route("/api/note/unpin", methods=["POST"])
@api_required
def api_unpin():
    note = _user_api_get_note(from_outbox=True)

    DB.activities.update_one(
        {"activity.object.id": note.id, "box": Box.OUTBOX.value},
        {"$set": {"meta.pinned": False}},
    )

    return _user_api_response(pinned=False)


@app.route("/api/undo", methods=["POST"])
@api_required
def api_undo():
    oid = _user_api_arg("id")
    doc = DB.activities.find_one(
        {
            "box": Box.OUTBOX.value,
            "$or": [{"remote_id": back.activity_url(oid)}, {"remote_id": oid}],
        }
    )
    if not doc:
        raise ActivityNotFoundError(f"cannot found {oid}")

    obj = ap.parse_activity(doc.get("activity"))
    # FIXME(tsileo): detect already undo-ed and make this API call idempotent
    undo = obj.build_undo()
    undo_id = post_to_outbox(undo)

    return _user_api_response(activity=undo_id)


@app.route("/admin/stream")
@login_required
def admin_stream():
    q = {"meta.stream": True, "meta.deleted": False}

    tpl = "stream.html"
    if request.args.get("debug"):
        tpl = "stream_debug.html"
        if request.args.get("debug_inbox"):
            q = {}

    inbox_data, older_than, newer_than = paginated_query(
        DB.activities, q, limit=int(request.args.get("limit", 25))
    )

    return render_template(
        tpl, inbox_data=inbox_data, older_than=older_than, newer_than=newer_than
    )


@app.route("/inbox", methods=["GET", "POST"])  # noqa: C901
def inbox():
    if request.method == "GET":
        if not is_api_request():
            abort(404)
        try:
            _api_required()
        except BadSignature:
            abort(404)

        return jsonify(
            **activitypub.build_ordered_collection(
                DB.activities,
                q={"meta.deleted": False, "box": Box.INBOX.value},
                cursor=request.args.get("cursor"),
                map_func=lambda doc: remove_context(doc["activity"]),
                col_name="inbox",
            )
        )

    data = request.get_json(force=True)
    print(f"req_headers={request.headers}")
    print(f"raw_data={data}")
    logger.debug(f"req_headers={request.headers}")
    logger.debug(f"raw_data={data}")
    try:
        if not verify_request(
            request.method, request.path, request.headers, request.data
        ):
            raise Exception("failed to verify request")
    except Exception:
        logger.exception(
            "failed to verify request, trying to verify the payload by fetching the remote"
        )
        try:
            data = get_backend().fetch_iri(data["id"])
        except ActivityGoneError:
            # XXX Mastodon sends Delete activities that are not dereferencable, it's the actor url with #delete
            # appended, so an `ActivityGoneError` kind of ensure it's "legit"
            if data["type"] == ActivityType.DELETE.value and data["id"].startswith(
                data["object"]
            ):
                logger.info(f"received a Delete for an actor {data!r}")
                if get_backend().inbox_check_duplicate(MY_PERSON, data["id"]):
                    # The activity is already in the inbox
                    logger.info(f"received duplicate activity {data!r}, dropping it")

                DB.activities.insert_one(
                    {
                        "box": Box.INBOX.value,
                        "activity": data,
                        "type": _to_list(data["type"]),
                        "remote_id": data["id"],
                        "meta": {"undo": False, "deleted": False},
                    }
                )
                # TODO(tsileo): write the callback the the delete external actor event
                return Response(status=201)
        except Exception:
            logger.exception(f'failed to fetch remote id at {data["id"]}')
            return Response(
                status=422,
                headers={"Content-Type": "application/json"},
                response=json.dumps(
                    {
                        "error": "failed to verify request (using HTTP signatures or fetching the IRI)"
                    }
                ),
            )
    print(data)
    activity = ap.parse_activity(data)
    logger.debug(f"inbox activity={activity}/{data}")
    post_to_inbox(activity)

    return Response(status=201)


def without_id(l):
    out = []
    for d in l:
        if "_id" in d:
            del d["_id"]
        out.append(d)
    return out


@app.route("/api/debug", methods=["GET", "DELETE"])
@api_required
def api_debug():
    """Endpoint used/needed for testing, only works in DEBUG_MODE."""
    if not DEBUG_MODE:
        return flask_jsonify(message="DEBUG_MODE is off")

    if request.method == "DELETE":
        _drop_db()
        return flask_jsonify(message="DB dropped")

    return flask_jsonify(
        inbox=DB.activities.count({"box": Box.INBOX.value}),
        outbox=DB.activities.count({"box": Box.OUTBOX.value}),
        outbox_data=without_id(DB.activities.find({"box": Box.OUTBOX.value})),
    )


@app.route("/api/new_note", methods=["POST"])
@api_required
def api_new_note():
    source = _user_api_arg("content")
    if not source:
        raise ValueError("missing content")

    _reply, reply = None, None
    try:
        _reply = _user_api_arg("reply")
    except ValueError:
        pass

    content, tags = parse_markdown(source)
    to = request.args.get("to")
    cc = [ID + "/followers"]

    if _reply:
        reply = ap.fetch_remote_activity(_reply)
        cc.append(reply.attributedTo)

    for tag in tags:
        if tag["type"] == "Mention":
            cc.append(tag["href"])

    raw_note = dict(
        attributedTo=MY_PERSON.id,
        cc=list(set(cc)),
        to=[to if to else ap.AS_PUBLIC],
        content=content,
        tag=tags,
        source={"mediaType": "text/markdown", "content": source},
        inReplyTo=reply.id if reply else None,
    )

    if "file" in request.files and request.files["file"].filename:
        file = request.files["file"]
        rfilename = secure_filename(file.filename)
        with BytesIO() as buf:
            file.save(buf)
            oid = MEDIA_CACHE.save_upload(buf, rfilename)
        mtype = mimetypes.guess_type(rfilename)[0]

        raw_note["attachment"] = [
            {
                "mediaType": mtype,
                "name": rfilename,
                "type": "Document",
                "url": f"{BASE_URL}/uploads/{oid}/{rfilename}",
            }
        ]

    note = ap.Note(**raw_note)
    create = note.build_create()
    create_id = post_to_outbox(create)

    return _user_api_response(activity=create_id)


@app.route("/api/new_article", methods=["POST"])
@api_required
def api_new_article():
    content = _user_api_arg("content")
    if not content:
        raise ValueError("missing content")

    name = _user_api_arg("name")
    if not name:
        raise ValueError("missing name")

    url = _user_api_arg("url")
    if not url:
        raise ValueError("missing url")

    _id = _user_api_arg("id")

    raw_article = dict(
        name=name,
        content=content,
        url=url,
        attributedTo=MY_PERSON.id,
        cc=[ID + "/followers"],
        to=[ap.AS_PUBLIC],
        tag=[],
        inReplyTo=None,
    )

    article = ap.Article(**raw_article)
    create = article.build_create()
    create_id = post_to_outbox(create, obj_id=_id)

    return _user_api_response(activity=create_id)


@app.route("/api/new_question", methods=["POST"])
@api_required
def api_new_question():
    source = _user_api_arg("content")
    if not source:
        raise ValueError("missing content")

    content, tags = parse_markdown(source)
    cc = [ID + "/followers"]

    for tag in tags:
        if tag["type"] == "Mention":
            cc.append(tag["href"])

    answers = []
    for i in range(4):
        a = _user_api_arg(f"answer{i}", default=None)
        if not a:
            break
        answers.append({"type": ActivityType.NOTE.value, "name": a})

    choices = {
        "endTime": ap.format_datetime(
            datetime.now().astimezone()
            + timedelta(minutes=int(_user_api_arg("open_for")))
        )
    }
    of = _user_api_arg("of")
    if of == "anyOf":
        choices["anyOf"] = answers
    else:
        choices["oneOf"] = answers

    raw_question = dict(
        attributedTo=MY_PERSON.id,
        cc=list(set(cc)),
        to=[ap.AS_PUBLIC],
        content=content,
        tag=tags,
        source={"mediaType": "text/markdown", "content": source},
        inReplyTo=None,
        **choices,
    )

    question = ap.Question(**raw_question)
    create = question.build_create()
    create_id = post_to_outbox(create)

    return _user_api_response(activity=create_id)


@app.route("/api/stream")
@api_required
def api_stream():
    return Response(
        response=json.dumps(
            activitypub.build_inbox_json_feed("/api/stream", request.args.get("cursor"))
        ),
        headers={"Content-Type": "application/json"},
    )


@app.route("/api/block", methods=["POST"])
@api_required
def api_block():
    actor = _user_api_arg("actor")

    existing = DB.activities.find_one(
        {
            "box": Box.OUTBOX.value,
            "type": ActivityType.BLOCK.value,
            "activity.object": actor,
            "meta.undo": False,
        }
    )
    if existing:
        return _user_api_response(activity=existing["activity"]["id"])

    block = ap.Block(actor=MY_PERSON.id, object=actor)
    block_id = post_to_outbox(block)

    return _user_api_response(activity=block_id)


@app.route("/api/follow", methods=["POST"])
@api_required
def api_follow():
    actor = _user_api_arg("actor")

    q = {
        "box": Box.OUTBOX.value,
        "type": ActivityType.FOLLOW.value,
        "meta.undo": False,
        "activity.object": actor,
    }

    existing = DB.activities.find_one(q)
    if existing:
        return _user_api_response(activity=existing["activity"]["id"])

    follow = ap.Follow(actor=MY_PERSON.id, object=actor)
    follow_id = post_to_outbox(follow)

    return _user_api_response(activity=follow_id)


@app.route("/followers")
def followers():
    q = {"box": Box.INBOX.value, "type": ActivityType.FOLLOW.value, "meta.undo": False}

    if is_api_request():
        return jsonify(
            **activitypub.build_ordered_collection(
                DB.activities,
                q=q,
                cursor=request.args.get("cursor"),
                map_func=lambda doc: doc["activity"]["actor"],
                col_name="followers",
            )
        )

    raw_followers, older_than, newer_than = paginated_query(DB.activities, q)
    followers = [
        doc["meta"]["actor"] for doc in raw_followers if "actor" in doc.get("meta", {})
    ]
    return render_template(
        "followers.html",
        followers_data=followers,
        older_than=older_than,
        newer_than=newer_than,
    )


@app.route("/following")
def following():
    q = {"box": Box.OUTBOX.value, "type": ActivityType.FOLLOW.value, "meta.undo": False}

    if is_api_request():
        return jsonify(
            **activitypub.build_ordered_collection(
                DB.activities,
                q=q,
                cursor=request.args.get("cursor"),
                map_func=lambda doc: doc["activity"]["object"],
                col_name="following",
            )
        )

    if config.HIDE_FOLLOWING and not session.get("logged_in", False):
        abort(404)

    following, older_than, newer_than = paginated_query(DB.activities, q)
    following = [
        (doc["remote_id"], doc["meta"]["object"])
        for doc in following
        if "remote_id" in doc and "object" in doc.get("meta", {})
    ]
    return render_template(
        "following.html",
        following_data=following,
        older_than=older_than,
        newer_than=newer_than,
    )


@app.route("/tags/<tag>")
def tags(tag):
    if not DB.activities.count(
        {
            "box": Box.OUTBOX.value,
            "activity.object.tag.type": "Hashtag",
            "activity.object.tag.name": "#" + tag,
        }
    ):
        abort(404)
    if not is_api_request():
        return render_template(
            "tags.html",
            tag=tag,
            outbox_data=DB.activities.find(
                {
                    "box": Box.OUTBOX.value,
                    "type": ActivityType.CREATE.value,
                    "meta.deleted": False,
                    "activity.object.tag.type": "Hashtag",
                    "activity.object.tag.name": "#" + tag,
                }
            ),
        )
    q = {
        "box": Box.OUTBOX.value,
        "meta.deleted": False,
        "meta.undo": False,
        "type": ActivityType.CREATE.value,
        "activity.object.tag.type": "Hashtag",
        "activity.object.tag.name": "#" + tag,
    }
    return jsonify(
        **activitypub.build_ordered_collection(
            DB.activities,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: doc["activity"]["object"]["id"],
            col_name=f"tags/{tag}",
        )
    )


@app.route("/featured")
def featured():
    if not is_api_request():
        abort(404)
    q = {
        "box": Box.OUTBOX.value,
        "type": ActivityType.CREATE.value,
        "meta.deleted": False,
        "meta.undo": False,
        "meta.pinned": True,
    }
    data = [clean_activity(doc["activity"]["object"]) for doc in DB.activities.find(q)]
    return jsonify(**activitypub.simple_build_ordered_collection("featured", data))


@app.route("/liked")
def liked():
    if not is_api_request():
        q = {
            "box": Box.OUTBOX.value,
            "type": ActivityType.LIKE.value,
            "meta.deleted": False,
            "meta.undo": False,
        }

        liked, older_than, newer_than = paginated_query(DB.activities, q)

        return render_template(
            "liked.html", liked=liked, older_than=older_than, newer_than=newer_than
        )

    q = {"meta.deleted": False, "meta.undo": False, "type": ActivityType.LIKE.value}
    return jsonify(
        **activitypub.build_ordered_collection(
            DB.activities,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: doc["activity"]["object"],
            col_name="liked",
        )
    )


#######
# IndieAuth


def build_auth_resp(payload):
    if request.headers.get("Accept") == "application/json":
        return Response(
            status=200,
            headers={"Content-Type": "application/json"},
            response=json.dumps(payload),
        )
    return Response(
        status=200,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
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
    for item in data["items"]:
        if "h-x-app" in item["type"] or "h-app" in item["type"]:
            props = item.get("properties", {})
            print(props)
            return dict(
                logo=_get_prop(props, "logo"),
                name=_get_prop(props, "name"),
                url=_get_prop(props, "url"),
            )
    return dict(logo=None, name=url, url=url)


@app.route("/indieauth/flow", methods=["POST"])
@login_required
def indieauth_flow():
    auth = dict(
        scope=" ".join(request.form.getlist("scopes")),
        me=request.form.get("me"),
        client_id=request.form.get("client_id"),
        state=request.form.get("state"),
        redirect_uri=request.form.get("redirect_uri"),
        response_type=request.form.get("response_type"),
        ts=datetime.now().timestamp(),
        code=binascii.hexlify(os.urandom(8)).decode("utf-8"),
        verified=False,
    )

    # XXX(tsileo): a whitelist for me values?

    # TODO(tsileo): redirect_uri checks
    if not auth["redirect_uri"]:
        abort(400)

    DB.indieauth.insert_one(auth)

    # FIXME(tsileo): fetch client ID and validate redirect_uri
    red = f'{auth["redirect_uri"]}?code={auth["code"]}&state={auth["state"]}&me={auth["me"]}'
    return redirect(red)


def _get_ip():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    geoip = None
    if request.headers.get("Broxy-Geoip-Country"):
        geoip = (
            request.headers.get("Broxy-Geoip-Country")
            + "/"
            + request.headers.get("Broxy-Geoip-Region")
        )
    return ip, geoip


@app.route("/indieauth", methods=["GET", "POST"])
def indieauth_endpoint():
    if request.method == "GET":
        if not session.get("logged_in"):
            return redirect(url_for("admin_login", next=request.url))

        me = request.args.get("me")
        # FIXME(tsileo): ensure me == ID
        client_id = request.args.get("client_id")
        redirect_uri = request.args.get("redirect_uri")
        state = request.args.get("state", "")
        response_type = request.args.get("response_type", "id")
        scope = request.args.get("scope", "").split()

        print("STATE", state)
        return render_template(
            "indieauth_flow.html",
            client=get_client_id_data(client_id),
            scopes=scope,
            redirect_uri=redirect_uri,
            state=state,
            response_type=response_type,
            client_id=client_id,
            me=me,
        )

    # Auth verification via POST
    code = request.form.get("code")
    redirect_uri = request.form.get("redirect_uri")
    client_id = request.form.get("client_id")

    ip, geoip = _get_ip()

    auth = DB.indieauth.find_one_and_update(
        {
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "verified": False,
        },
        {
            "$set": {
                "verified": True,
                "verified_by": "id",
                "verified_at": datetime.now().timestamp(),
                "ip_address": ip,
                "geoip": geoip,
            }
        },
    )
    print(auth)
    print(code, redirect_uri, client_id)

    # Ensure the code is recent
    if (datetime.now() - datetime.fromtimestamp(auth["ts"])) > timedelta(minutes=5):
        abort(400)

    if not auth:
        abort(403)
        return

    session["logged_in"] = True
    me = auth["me"]
    state = auth["state"]
    scope = auth["scope"]
    print("STATE", state)
    return build_auth_resp({"me": me, "state": state, "scope": scope})


@app.route("/token", methods=["GET", "POST"])
def token_endpoint():
    # Generate a new token with the returned access code
    if request.method == "POST":
        code = request.form.get("code")
        me = request.form.get("me")
        redirect_uri = request.form.get("redirect_uri")
        client_id = request.form.get("client_id")

        now = datetime.now()
        ip, geoip = _get_ip()

        # This query ensure code, client_id, redirect_uri and me are matching with the code request
        auth = DB.indieauth.find_one_and_update(
            {
                "code": code,
                "me": me,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "verified": False,
            },
            {
                "$set": {
                    "verified": True,
                    "verified_by": "code",
                    "verified_at": now.timestamp(),
                    "ip_address": ip,
                    "geoip": geoip,
                }
            },
        )

        if not auth:
            abort(403)

        scope = auth["scope"].split()

        # Ensure there's at least one scope
        if not len(scope):
            abort(400)

        # Ensure the code is recent
        if (now - datetime.fromtimestamp(auth["ts"])) > timedelta(minutes=5):
            abort(400)

        payload = dict(me=me, client_id=client_id, scope=scope, ts=now.timestamp())
        token = JWT.dumps(payload).decode("utf-8")
        DB.indieauth.update_one(
            {"_id": auth["_id"]},
            {
                "$set": {
                    "token": token,
                    "token_expires": (now + timedelta(minutes=30)).timestamp(),
                }
            },
        )

        return build_auth_resp(
            {"me": me, "scope": auth["scope"], "access_token": token}
        )

    # Token verification
    token = request.headers.get("Authorization").replace("Bearer ", "")
    try:
        payload = JWT.loads(token)
    except BadSignature:
        abort(403)

    # Check the token expritation (valid for 3 hours)
    if (datetime.now() - datetime.fromtimestamp(payload["ts"])) > timedelta(
        minutes=180
    ):
        abort(401)

    return build_auth_resp(
        {
            "me": payload["me"],
            "scope": " ".join(payload["scope"]),
            "client_id": payload["client_id"],
        }
    )


#################
# Feeds


@app.route("/feed.json")
def json_feed():
    return Response(
        response=json.dumps(activitypub.json_feed("/feed.json")),
        headers={"Content-Type": "application/json"},
    )


@app.route("/feed.atom")
def atom_feed():
    return Response(
        response=activitypub.gen_feed().atom_str(),
        headers={"Content-Type": "application/atom+xml"},
    )


@app.route("/feed.rss")
def rss_feed():
    return Response(
        response=activitypub.gen_feed().rss_str(),
        headers={"Content-Type": "application/rss+xml"},
    )


###########
# Tasks


class Tasks:
    @staticmethod
    def cache_object(iri: str) -> None:
        p.push(iri, "/task/cache_object")

    @staticmethod
    def cache_actor(iri: str, also_cache_attachments: bool = True) -> None:
        p.push(
            {"iri": iri, "also_cache_attachments": also_cache_attachments},
            "/task/cache_actor",
        )

    @staticmethod
    def post_to_remote_inbox(payload: str, recp: str) -> None:
        p.push({"payload": payload, "to": recp}, "/task/post_to_remote_inbox")

    @staticmethod
    def forward_activity(iri: str) -> None:
        p.push(iri, "/task/forward_activity")

    @staticmethod
    def fetch_og_meta(iri: str) -> None:
        p.push(iri, "/task/fetch_og_meta")

    @staticmethod
    def process_new_activity(iri: str) -> None:
        p.push(iri, "/task/process_new_activity")

    @staticmethod
    def cache_attachments(iri: str) -> None:
        p.push(iri, "/task/cache_attachments")

    @staticmethod
    def finish_post_to_inbox(iri: str) -> None:
        p.push(iri, "/task/finish_post_to_inbox")

    @staticmethod
    def finish_post_to_outbox(iri: str) -> None:
        p.push(iri, "/task/finish_post_to_outbox")


@app.route("/task/fetch_og_meta", methods=["POST"])
def task_fetch_og_meta():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")
        if activity.has_type(ap.ActivityType.CREATE):
            note = activity.get_object()
            links = opengraph.links_from_note(note.to_dict())
            og_metadata = opengraph.fetch_og_metadata(USER_AGENT, links)
            for og in og_metadata:
                if not og.get("image"):
                    continue
                MEDIA_CACHE.cache_og_image2(og["image"], iri)

            app.logger.debug(f"OG metadata {og_metadata!r}")
            DB.activities.update_one(
                {"remote_id": iri}, {"$set": {"meta.og_metadata": og_metadata}}
            )

        app.logger.info(f"OG metadata fetched for {iri}")
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"dropping activity {iri}, skip OG metedata")
        return ""
    except requests.exceptions.HTTPError as http_err:
        if 400 <= http_err.response.status_code < 500:
            app.logger.exception("bad request, no retry")
            return ""
        app.logger.exception("failed to fetch OG metadata")
        raise TaskError() from http_err
    except Exception as err:
        app.logger.exception(f"failed to fetch OG metadata for {iri}")
        raise TaskError() from err

    return ""


@app.route("/task/cache_object", methods=["POST"])
def task_cache_object():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")
        obj = activity.get_object()
        DB.activities.update_one(
            {"remote_id": activity.id},
            {
                "$set": {
                    "meta.object": obj.to_dict(embed=True),
                    "meta.object_actor": activitypub._actor_to_meta(obj.get_actor()),
                }
            },
        )
    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        DB.activities.update_one({"remote_id": iri}, {"$set": {"meta.deleted": True}})
        app.logger.exception(f"flagging activity {iri} as deleted, no object caching")
    except Exception as err:
        app.logger.exception(f"failed to cache object for {iri}")
        raise TaskError() from err

    return ""


@app.route("/task/finish_post_to_outbox", methods=["POST"])  # noqa:C901
def task_finish_post_to_outbox():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        recipients = activity.recipients()

        if activity.has_type(ap.ActivityType.DELETE):
            back.outbox_delete(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.UPDATE):
            back.outbox_update(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.CREATE):
            back.outbox_create(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.ANNOUNCE):
            back.outbox_announce(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.LIKE):
            back.outbox_like(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.UNDO):
            obj = activity.get_object()
            if obj.has_type(ap.ActivityType.LIKE):
                back.outbox_undo_like(MY_PERSON, obj)
            elif obj.has_type(ap.ActivityType.ANNOUNCE):
                back.outbox_undo_announce(MY_PERSON, obj)
            elif obj.has_type(ap.ActivityType.FOLLOW):
                back.undo_new_following(MY_PERSON, obj)

        app.logger.info(f"recipients={recipients}")
        activity = ap.clean_activity(activity.to_dict())

        DB.cache2.remove()

        payload = json.dumps(activity)
        for recp in recipients:
            app.logger.debug(f"posting to {recp}")
            Tasks.post_to_remote_inbox(payload, recp)
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"no retry")
    except Exception as err:
        app.logger.exception(f"failed to post to remote inbox for {iri}")
        raise TaskError() from err

    return ""


@app.route("/task/finish_post_to_inbox", methods=["POST"])  # noqa: C901
def task_finish_post_to_inbox():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        if activity.has_type(ap.ActivityType.DELETE):
            back.inbox_delete(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.UPDATE):
            back.inbox_update(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.CREATE):
            back.inbox_create(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.ANNOUNCE):
            back.inbox_announce(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.LIKE):
            back.inbox_like(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.FOLLOW):
            # Reply to a Follow with an Accept
            accept = ap.Accept(actor=ID, object=activity.to_dict(embed=True))
            post_to_outbox(accept)
        elif activity.has_type(ap.ActivityType.UNDO):
            obj = activity.get_object()
            if obj.has_type(ap.ActivityType.LIKE):
                back.inbox_undo_like(MY_PERSON, obj)
            elif obj.has_type(ap.ActivityType.ANNOUNCE):
                back.inbox_undo_announce(MY_PERSON, obj)
            elif obj.has_type(ap.ActivityType.FOLLOW):
                back.undo_new_follower(MY_PERSON, obj)
        try:
            invalidate_cache(activity)
        except Exception:
            app.logger.exception("failed to invalidate cache")
    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        app.logger.exception(f"no retry")
    except Exception as err:
        app.logger.exception(f"failed to cache attachments for {iri}")
        raise TaskError() from err

    return ""


def post_to_outbox(activity: ap.BaseActivity, obj_id: Optional[str] = None) -> str:
    if activity.has_type(ap.CREATE_TYPES):
        activity = activity.build_create()

    # Assign create a random ID
    if obj_id is None:
        obj_id = back.random_object_id()

    activity.set_id(back.activity_url(obj_id), obj_id)

    back.save(Box.OUTBOX, activity)
    Tasks.cache_actor(activity.id)
    Tasks.finish_post_to_outbox(activity.id)
    return activity.id


def post_to_inbox(activity: ap.BaseActivity) -> None:
    # Check for Block activity
    actor = activity.get_actor()
    if back.outbox_is_blocked(MY_PERSON, actor.id):
        app.logger.info(
            f"actor {actor!r} is blocked, dropping the received activity {activity!r}"
        )
        return

    if back.inbox_check_duplicate(MY_PERSON, activity.id):
        # The activity is already in the inbox
        app.logger.info(f"received duplicate activity {activity!r}, dropping it")

    back.save(Box.INBOX, activity)
    Tasks.process_new_activity(activity.id)

    app.logger.info(f"spawning task for {activity!r}")
    Tasks.finish_post_to_inbox(activity.id)


def invalidate_cache(activity):
    if activity.has_type(ap.ActivityType.LIKE):
        if activity.get_object().id.startswith(BASE_URL):
            DB.cache2.remove()
    elif activity.has_type(ap.ActivityType.ANNOUNCE):
        if activity.get_object().id.startswith(BASE_URL):
            DB.cache2.remove()
    elif activity.has_type(ap.ActivityType.UNDO):
        DB.cache2.remove()
    elif activity.has_type(ap.ActivityType.DELETE):
        # TODO(tsileo): only invalidate if it's a delete of a reply
        DB.cache2.remove()
    elif activity.has_type(ap.ActivityType.UPDATE):
        DB.cache2.remove()
    elif activity.has_type(ap.ActivityType.CREATE):
        note = activity.get_object()
        in_reply_to = note.get_in_reply_to()
        if not in_reply_to or in_reply_to.startswith(ID):
            DB.cache2.remove()
        # FIXME(tsileo): check if it's a reply of a reply


@app.route("/task/cache_attachments", methods=["POST"])
def task_cache_attachments():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")
        # Generates thumbnails for the actor's icon and the attachments if any

        actor = activity.get_actor()

        # Update the cached actor
        DB.actors.update_one(
            {"remote_id": iri},
            {"$set": {"remote_id": iri, "data": actor.to_dict(embed=True)}},
            upsert=True,
        )

        if actor.icon:
            MEDIA_CACHE.cache(actor.icon["url"], Kind.ACTOR_ICON)

        if activity.has_type(ap.ActivityType.CREATE):
            for attachment in activity.get_object()._data.get("attachment", []):
                if (
                    attachment.get("mediaType", "").startswith("image/")
                    or attachment.get("type") == ap.ActivityType.IMAGE.value
                ):
                    try:
                        MEDIA_CACHE.cache_attachment2(attachment["url"], iri)
                    except ValueError:
                        app.logger.exception(f"failed to cache {attachment}")

        app.logger.info(f"attachments cached for {iri}")

    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        app.logger.exception(f"dropping activity {iri}, no attachment caching")
    except Exception as err:
        app.logger.exception(f"failed to cache attachments for {iri}")
        raise TaskError() from err

    return ""


@app.route("/task/cache_actor", methods=["POST"])
def task_cache_actor() -> str:
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri, also_cache_attachments = (
        task.payload["iri"],
        task.payload.get("also_cache_attachments", True),
    )
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        if activity.has_type(ap.ActivityType.CREATE):
            Tasks.fetch_og_meta(iri)

        if activity.has_type([ap.ActivityType.LIKE, ap.ActivityType.ANNOUNCE]):
            Tasks.cache_object(iri)

        actor = activity.get_actor()

        cache_actor_with_inbox = False
        if activity.has_type(ap.ActivityType.FOLLOW):
            if actor.id != ID:
                # It's a Follow from the Inbox
                cache_actor_with_inbox = True
            else:
                # It's a new following, cache the "object" (which is the actor we follow)
                DB.activities.update_one(
                    {"remote_id": iri},
                    {
                        "$set": {
                            "meta.object": activitypub._actor_to_meta(
                                activity.get_object()
                            )
                        }
                    },
                )

        # Cache the actor info
        DB.activities.update_one(
            {"remote_id": iri},
            {
                "$set": {
                    "meta.actor": activitypub._actor_to_meta(
                        actor, cache_actor_with_inbox
                    )
                }
            },
        )

        app.logger.info(f"actor cached for {iri}")
        if also_cache_attachments and activity.has_type(ap.ActivityType.CREATE):
            Tasks.cache_attachments(iri)

    except (ActivityGoneError, ActivityNotFoundError):
        DB.activities.update_one({"remote_id": iri}, {"$set": {"meta.deleted": True}})
        app.logger.exception(f"flagging activity {iri} as deleted, no actor caching")
    except Exception as err:
        app.logger.exception(f"failed to cache actor for {iri}")
        raise TaskError() from err

    return ""


@app.route("/task/process_new_activity", methods=["POST"])  # noqa:c901
def task_process_new_activity():
    """Process an activity received in the inbox"""
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        # Is the activity expected?
        # following = ap.get_backend().following()
        should_forward = False
        should_delete = False
        should_keep = False

        tag_stream = False
        if activity.has_type(ap.ActivityType.ANNOUNCE):
            # FIXME(tsileo): Ensure it's follower and store into a "dead activities" DB
            try:
                activity.get_object()
                tag_stream = True
                if activity.get_object_id().startswith(BASE_URL):
                    should_keep = True
            except (NotAnActivityError, BadActivityError):
                app.logger.exception(f"failed to get announce object for {activity!r}")
                # Most likely on OStatus notice
                tag_stream = False
                should_delete = True
            except (ActivityGoneError, ActivityNotFoundError):
                # The announced activity is deleted/gone, drop it
                should_delete = True

        elif activity.has_type(ap.ActivityType.FOLLOW):
            # FIXME(tsileo): ensure it's a follow where the server is the object
            should_keep = True

        elif activity.has_type(ap.ActivityType.CREATE):
            note = activity.get_object()
            in_reply_to = note.get_in_reply_to()
            # Make the note part of the stream if it's not a reply, or if it's a local reply
            if not in_reply_to or in_reply_to.startswith(ID):
                tag_stream = True

            # FIXME(tsileo): check for direct addressing in the to, cc, bcc... fields
            if (in_reply_to and in_reply_to.startswith(ID)) or note.has_mention(ID):
                should_keep = True

            if in_reply_to:
                try:
                    reply = ap.fetch_remote_activity(note.get_in_reply_to())
                    if (
                        reply.id.startswith(ID) or reply.has_mention(ID)
                    ) and activity.is_public():
                        # The reply is public "local reply", forward the reply (i.e. the original activity) to the
                        # original recipients
                        should_forward = True
                        should_keep = True
                except NotAnActivityError:
                    # Most likely a reply to an OStatus notce
                    should_delete = True

            # (partial) Ghost replies handling
            # [X] This is the first time the server has seen this Activity.
            should_forward = False
            local_followers = ID + "/followers"
            for field in ["to", "cc"]:
                if field in activity._data:
                    if local_followers in activity._data[field]:
                        # [X] The values of to, cc, and/or audience contain a Collection owned by the server.
                        should_forward = True

            # [X] The values of inReplyTo, object, target and/or tag are objects owned by the server
            if not (in_reply_to and in_reply_to.startswith(ID)):
                should_forward = False

        elif activity.has_type(ap.ActivityType.DELETE):
            note = DB.activities.find_one(
                {"activity.object.id": activity.get_object().id}
            )
            if note and note["meta"].get("forwarded", False):
                # If the activity was originally forwarded, forward the delete too
                should_forward = True

        elif activity.has_type(ap.ActivityType.LIKE):
            if activity.get_object_id().startswith(BASE_URL):
                should_keep = True
            else:
                # We only want to keep a like if it's a like for a local activity
                # (Pleroma relay the likes it received, we don't want to store them)
                should_delete = True

        if should_forward:
            app.logger.info(f"will forward {activity!r} to followers")
            Tasks.forward_activity(activity.id)

        if should_delete:
            app.logger.info(f"will soft delete {activity!r}")

        app.logger.info(f"{iri} tag_stream={tag_stream}")
        DB.activities.update_one(
            {"remote_id": activity.id},
            {
                "$set": {
                    "meta.keep": should_keep,
                    "meta.stream": tag_stream,
                    "meta.forwarded": should_forward,
                    "meta.deleted": should_delete,
                }
            },
        )

        app.logger.info(f"new activity {iri} processed")
        if not should_delete and not activity.has_type(ap.ActivityType.DELETE):
            Tasks.cache_actor(iri)
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"dropping activity {iri}, skip processing")
        return ""
    except Exception as err:
        app.logger.exception(f"failed to process new activity {iri}")
        raise TaskError() from err

    return ""


@app.route("/task/forward_activity", methods=["POST"])
def task_forward_activity():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        recipients = back.followers_as_recipients()
        app.logger.debug(f"Forwarding {activity!r} to {recipients}")
        activity = ap.clean_activity(activity.to_dict())
        payload = json.dumps(activity)
        for recp in recipients:
            app.logger.debug(f"forwarding {activity!r} to {recp}")
            Tasks.post_to_remote_inbox(payload, recp)
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@app.route("/task/post_to_remote_inbox", methods=["POST"])
def task_post_to_remote_inbox():
    """Post an activity to a remote inbox."""
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    payload, to = task.payload["payload"], task.payload["to"]
    try:
        app.logger.info("payload=%s", payload)
        app.logger.info("generating sig")
        signed_payload = json.loads(payload)

        # XXX Disable JSON-LD signature crap for now (as HTTP signatures are enough for most implementations)
        # Don't overwrite the signature if we're forwarding an activity
        # if "signature" not in signed_payload:
        #    generate_signature(signed_payload, KEY)

        app.logger.info("to=%s", to)
        resp = requests.post(
            to,
            data=json.dumps(signed_payload),
            auth=SIG_AUTH,
            headers={
                "Content-Type": HEADERS[1],
                "Accept": HEADERS[1],
                "User-Agent": USER_AGENT,
            },
        )
        app.logger.info("resp=%s", resp)
        app.logger.info("resp_body=%s", resp.text)
        resp.raise_for_status()
    except HTTPError as err:
        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@app.route("/task/update_question", methods=["POST"])
def task_update_question():
    """Post an activity to a remote inbox."""
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        app.logger.info(f"Updating question {iri}")
        # TODO(tsileo): sends an Update with the question/iri as an actor, with the updated stats (LD sig will fail?)
        # but to who? followers and people who voted? but this must not be visible right?
        # also sends/trigger a notification when a poll I voted for ends like Mastodon?
    except HTTPError as err:
        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@app.route("/task/cleanup", methods=["POST"])
def task_cleanup():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    p.push({}, "/task/cleanup_part_1")
    return ""


@app.route("/task/cleanup_part_1", methods=["POST"])
def task_cleanup_part_1():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    d = (datetime.utcnow() - timedelta(days=15)).strftime("%Y-%m-%d")

    # (We keep Follow and Accept forever)

    # Announce and Like cleanup
    for ap_type in [ActivityType.ANNOUNCE, ActivityType.LIKE]:
        # Migrate old (before meta.keep activities on the fly)
        DB.activities.update_many(
            {
                "box": Box.INBOX.value,
                "type": ap_type.value,
                "meta.keep": {"$exists": False},
                "activity.object": {"$regex": f"^{BASE_URL}"},
            },
            {"$set": {"meta.keep": True}},
        )

        DB.activities.update_many(
            {
                "box": Box.INBOX.value,
                "type": ap_type.value,
                "meta.keep": {"$exists": False},
                "activity.object.id": {"$regex": f"^{BASE_URL}"},
            },
            {"$set": {"meta.keep": True}},
        )

        DB.activities.update_many(
            {
                "box": Box.INBOX.value,
                "type": ap_type.value,
                "meta.keep": {"$exists": False},
            },
            {"$set": {"meta.keep": False}},
        )
        # End of the migration

        # Delete old activities
        DB.activities.delete_many(
            {
                "box": Box.INBOX.value,
                "type": ap_type.value,
                "meta.keep": False,
                "activity.published": {"$lt": d},
            }
        )

        # And delete the soft-deleted one
        DB.activities.delete_many(
            {
                "box": Box.INBOX.value,
                "type": ap_type.value,
                "meta.keep": False,
                "meta.deleted": True,
            }
        )

    # Create cleanup (more complicated)
    # The one that mention our actor
    DB.activities.update_many(
        {
            "box": Box.INBOX.value,
            "meta.keep": {"$exists": False},
            "activity.object.tag.href": {"$regex": f"^{BASE_URL}"},
        },
        {"$set": {"meta.keep": True}},
    )
    DB.activities.update_many(
        {
            "box": Box.REPLIES.value,
            "meta.keep": {"$exists": False},
            "activity.tag.href": {"$regex": f"^{BASE_URL}"},
        },
        {"$set": {"meta.keep": True}},
    )

    # The replies of the outbox
    DB.activities.update_many(
        {"meta.thread_root_parent": {"$regex": f"^{BASE_URL}"}},
        {"$set": {"meta.keep": True}},
    )
    # Track all the threads we participated
    keep_threads = []
    for data in DB.activities.find(
        {
            "box": Box.OUTBOX.value,
            "type": ActivityType.CREATE.value,
            "meta.thread_root_parent": {"$exists": True},
        }
    ):
        keep_threads.append(data["meta"]["thread_root_parent"])

    for root_parent in set(keep_threads):
        DB.activities.update_many(
            {"meta.thread_root_parent": root_parent}, {"$set": {"meta.keep": True}}
        )

    DB.activities.update_many(
        {
            "box": {"$in": [Box.REPLIES.value, Box.INBOX.value]},
            "meta.keep": {"$exists": False},
        },
        {"$set": {"meta.keep": False}},
    )

    DB.activities.update_many(
        {
            "box": Box.OUTBOX.value,
            "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
            "meta.public": {"$exists": False},
        },
        {"$set": {"meta.public": True}},
    )

    p.push({}, "/task/cleanup_part_2")
    return "OK"


@app.route("/task/cleanup_part_2", methods=["POST"])
def task_cleanup_part_2():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")
    d = (datetime.utcnow() - timedelta(days=15)).strftime("%Y-%m-%d")

    # Go over the old Create activities
    for data in DB.activities.find(
        {
            "box": Box.INBOX.value,
            "type": ActivityType.CREATE.value,
            "meta.keep": False,
            "activity.published": {"$lt": d},
        }
    ).limit(5000):
        # Delete the cached attachment/
        for grid_item in MEDIA_CACHE.fs.find({"remote_id": data["remote_id"]}):
            MEDIA_CACHE.fs.delete(grid_item._id)
        DB.activities.delete_one({"_id": data["_id"]})

    p.push({}, "/task/cleanup_part_3")
    return "OK"


@app.route("/task/cleanup_part_3", methods=["POST"])
def task_cleanup_part_3():
    task = p.parse(request)
    app.logger.info(f"task={task!r}")

    d = (datetime.utcnow() - timedelta(days=15)).strftime("%Y-%m-%d")

    # Delete old replies we don't care about
    DB.activities.delete_many(
        {"box": Box.REPLIES.value, "meta.keep": False, "activity.published": {"$lt": d}}
    )

    # Remove all the attachments no tied to a remote_id (post celery migration)
    for grid_item in MEDIA_CACHE.fs.find(
        {"kind": {"$in": ["og", "attachment"]}, "remote_id": {"$exists": False}}
    ):
        MEDIA_CACHE.fs.delete(grid_item._id)

    # TODO(tsileo): iterator over "actor_icon" and look for unused one in a separate task

    return "OK"
