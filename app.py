import json
import logging
import os
import traceback
from datetime import datetime
from urllib.parse import urlparse

from bson.objectid import ObjectId
from flask import Flask
from flask import Response
from flask import abort
from flask import jsonify as flask_jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from itsdangerous import BadSignature
from little_boxes import activitypub as ap
from little_boxes.activitypub import ActivityType
from little_boxes.activitypub import activity_from_doc
from little_boxes.activitypub import clean_activity
from little_boxes.activitypub import get_backend
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import Error
from little_boxes.httpsig import verify_request
from little_boxes.webfinger import get_actor_url
from little_boxes.webfinger import get_remote_follow_template
from u2flib_server import u2f

import blueprints.admin
import blueprints.indieauth
import blueprints.tasks
import blueprints.well_known
import config
from blueprints.api import _api_required
from blueprints.tasks import TaskError
from config import DB
from config import HEADERS
from config import ID
from config import ME
from config import MEDIA_CACHE
from config import VERSION
from core import activitypub
from core import feed
from core.activitypub import activity_url
from core.activitypub import post_to_inbox
from core.activitypub import post_to_outbox
from core.activitypub import remove_context
from core.db import find_one_activity
from core.meta import Box
from core.meta import MetaKey
from core.meta import _meta
from core.meta import by_remote_id
from core.meta import in_outbox
from core.meta import is_public
from core.shared import MY_PERSON
from core.shared import _build_thread
from core.shared import _get_ip
from core.shared import csrf
from core.shared import login_required
from core.shared import noindex
from core.shared import paginated_query
from utils import now
from utils.key import get_secret_key
from utils.template_filters import filters

app = Flask(__name__)
app.secret_key = get_secret_key("flask")
app.register_blueprint(filters)
app.register_blueprint(blueprints.admin.blueprint)
app.register_blueprint(blueprints.api.blueprint, url_prefix="/api")
app.register_blueprint(blueprints.indieauth.blueprint)
app.register_blueprint(blueprints.tasks.blueprint)
app.register_blueprint(blueprints.well_known.blueprint)
app.config.update(WTF_CSRF_CHECK_DEFAULT=False)
csrf.init_app(app)

logger = logging.getLogger(__name__)

# Hook up Flask logging with gunicorn
root_logger = logging.getLogger()
if os.getenv("FLASK_DEBUG"):
    logger.setLevel(logging.DEBUG)
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers = app.logger.handlers
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    root_logger.handlers = gunicorn_logger.handlers
    root_logger.setLevel(gunicorn_logger.level)


def is_blacklisted(url: str) -> bool:
    try:
        return urlparse(url).netloc in config.BLACKLIST
    except Exception:
        logger.exception(f"failed to blacklist for {url}")
        return False


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
    # FIXME(tsileo): rename to all_count, and remove poll answers from it
    all_q = {
        "box": Box.OUTBOX.value,
        "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
        "meta.undo": False,
        "meta.deleted": False,
        "meta.poll_answer": False,
    }
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
    unread_notifications_q = {_meta(MetaKey.NOTIFICATION_UNREAD): True}

    logged_in = session.get("logged_in", False)

    return dict(
        microblogpub_version=VERSION,
        config=config,
        logged_in=logged_in,
        followers_count=DB.activities.count(followers_q),
        following_count=DB.activities.count(following_q) if logged_in else 0,
        notes_count=notes_count,
        liked_count=liked_count,
        with_replies_count=DB.activities.count(all_q) if logged_in else 0,
        unread_notifications_count=DB.activities.count(unread_notifications_q)
        if logged_in
        else 0,
        me=ME,
        base_url=config.BASE_URL,
    )


@app.after_request
def set_x_powered_by(response):
    response.headers["X-Powered-By"] = "microblog.pub"
    return response


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


@app.route("/remote_follow", methods=["GET", "POST"])
def remote_follow():
    if request.method == "GET":
        return render_template("remote_follow.html")

    csrf.protect()
    profile = request.form.get("profile")
    if not profile.startswith("@"):
        profile = f"@{profile}"
    return redirect(get_remote_follow_template(profile).format(uri=ID))


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

    follow = ap.Follow(
        actor=MY_PERSON.id, object=actor, to=[actor], cc=[ap.AS_PUBLIC], published=now()
    )
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


@app.route("/")
def index():
    if is_api_request():
        return jsonify(**ME)

    q = {
        "box": Box.OUTBOX.value,
        "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
        "activity.object.inReplyTo": None,
        "meta.deleted": False,
        "meta.undo": False,
        "meta.public": True,
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
            "meta.public": True,
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
    return resp


@app.route("/all")
@login_required
def all():
    q = {
        "box": Box.OUTBOX.value,
        "type": {"$in": [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
        "meta.deleted": False,
        "meta.undo": False,
        "meta.poll_answer": False,
    }
    outbox_data, older_than, newer_than = paginated_query(DB.activities, q)

    return render_template(
        "index.html",
        outbox_data=outbox_data,
        older_than=older_than,
        newer_than=newer_than,
    )


@app.route("/note/<note_id>")
def note_by_id(note_id):
    if is_api_request():
        return redirect(url_for("outbox_activity", item_id=note_id))

    data = DB.activities.find_one(
        {"box": Box.OUTBOX.value, "remote_id": activity_url(note_id)}
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


@app.route("/outbox", methods=["GET", "POST"])
def outbox():
    if request.method == "GET":
        if not is_api_request():
            abort(404)
        # TODO(tsileo): returns the whole outbox if authenticated and look at OCAP support
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
        {
            "box": Box.OUTBOX.value,
            "remote_id": activity_url(item_id),
            "meta.public": True,
        }
    )
    if not doc:
        abort(404)

    if doc["meta"].get("deleted", False):
        abort(404)

    return jsonify(**activity_from_doc(doc))


@app.route("/outbox/<item_id>/activity")
def outbox_activity(item_id):
    data = find_one_activity(
        {**in_outbox(), **by_remote_id(activity_url(item_id)), **is_public()}
    )
    if not data:
        abort(404)

    obj = activity_from_doc(data)
    if data["meta"].get("deleted", False):
        abort(404)

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
            "remote_id": activity_url(item_id),
            "meta.deleted": False,
            "meta.public": True,
        }
    )
    if not data:
        abort(404)
    obj = ap.parse_activity(data["activity"])
    if obj.ACTIVITY_TYPE != ActivityType.CREATE:
        abort(404)

    q = {
        "meta.deleted": False,
        "meta.public": True,
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
            "remote_id": activity_url(item_id),
            "meta.deleted": False,
            "meta.public": True,
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
            "remote_id": activity_url(item_id),
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


@app.route("/inbox", methods=["GET", "POST"])  # noqa: C901
def inbox():
    # GET /inbox
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

    # POST/ inbox
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
    except Exception:
        return Response(
            status=422,
            headers={"Content-Type": "application/json"},
            response=json.dumps({"error": "failed to decode request as JSON"}),
        )

    # Check the blacklist now to see if we can return super early
    if (
        "id" in data
        and is_blacklisted(data["id"])
        or (
            "object" in data
            and isinstance(data["object"], dict)
            and "id" in data["object"]
            and is_blacklisted(data["object"]["id"])
        )
        or (
            "object" in data
            and isinstance(data["object"], str)
            and is_blacklisted(data["object"])
        )
    ):
        logger.info(f"dropping activity from blacklisted host: {data['id']}")
        return Response(status=201)

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
            remote_data = get_backend().fetch_iri(data["id"])
        except ActivityGoneError:
            # XXX Mastodon sends Delete activities that are not dereferencable, it's the actor url with #delete
            # appended, so an `ActivityGoneError` kind of ensure it's "legit"
            if data["type"] == ActivityType.DELETE.value and data["id"].startswith(
                data["object"]
            ):
                # If we're here, this means the key is not saved, so we cannot verify the object
                logger.info(f"received a Delete for an unknown actor {data!r}, drop it")

                return Response(status=201)
        except Exception:
            logger.exception(f"failed to fetch remote for payload {data!r}")

            if "type" in data:
                # Friendica does not returns a 410, but a 302 that redirect to an HTML page
                if ap._has_type(data["type"], ActivityType.DELETE):
                    logger.info(
                        f"received a Delete for an unknown actor {data!r}, drop it"
                    )
                    return Response(status=201)

            if "id" in data:
                if DB.trash.find_one({"activity.id": data["id"]}):
                    # It's already stored in trash, returns early
                    return Response(
                        status=422,
                        headers={"Content-Type": "application/json"},
                        response=json.dumps(
                            {
                                "error": "failed to verify request (using HTTP signatures or fetching the IRI)"
                            }
                        ),
                    )

            # Now we can store this activity in the trash for later analysis

            # Track/store the payload for analysis
            ip, geoip = _get_ip()

            DB.trash.insert(
                {
                    "activity": data,
                    "meta": {
                        "ts": datetime.now().timestamp(),
                        "ip_address": ip,
                        "geoip": geoip,
                        "tb": traceback.format_exc(),
                        "headers": dict(request.headers),
                    },
                }
            )

            return Response(
                status=422,
                headers={"Content-Type": "application/json"},
                response=json.dumps(
                    {
                        "error": "failed to verify request (using HTTP signatures or fetching the IRI)"
                    }
                ),
            )

        # We fetched the remote data successfully
        data = remote_data
    print(data)
    activity = ap.parse_activity(data)
    logger.debug(f"inbox activity={activity}/{data}")
    post_to_inbox(activity)

    return Response(status=201)


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
    lists = list(DB.lists.find())
    return render_template(
        "following.html",
        following_data=following,
        older_than=older_than,
        newer_than=newer_than,
        lists=lists,
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


#################
# Feeds


@app.route("/feed.json")
def json_feed():
    return Response(
        response=json.dumps(feed.json_feed("/feed.json")),
        headers={"Content-Type": "application/json"},
    )


@app.route("/feed.atom")
def atom_feed():
    return Response(
        response=feed.gen_feed().atom_str(),
        headers={"Content-Type": "application/atom+xml"},
    )


@app.route("/feed.rss")
def rss_feed():
    return Response(
        response=feed.gen_feed().rss_str(),
        headers={"Content-Type": "application/rss+xml"},
    )
