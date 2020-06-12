import json
import logging
import os
import traceback
from datetime import datetime
from typing import Any
from uuid import uuid4

import requests
from bson.errors import InvalidId
from bson.objectid import ObjectId
from flask import Flask
from flask import Response
from flask import abort
from flask import g
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from gridfs.errors import NoFile
from itsdangerous import BadSignature
from little_boxes import activitypub as ap
from little_boxes.activitypub import ActivityType
from little_boxes.activitypub import clean_activity
from little_boxes.activitypub import get_backend
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import Error
from little_boxes.httpsig import verify_request
from little_boxes.webfinger import get_remote_follow_template
from werkzeug.exceptions import InternalServerError

import blueprints.admin
import blueprints.indieauth
import blueprints.tasks
import blueprints.well_known
import config
from blueprints.api import _api_required
from blueprints.api import api_required
from blueprints.tasks import TaskError
from config import DB
from config import ID
from config import ME
from config import MEDIA_CACHE
from config import VERSION
from core import activitypub
from core import feed
from core import jsonld
from core.activitypub import activity_from_doc
from core.activitypub import activity_url
from core.activitypub import post_to_inbox
from core.activitypub import post_to_outbox
from core.activitypub import remove_context
from core.db import find_one_activity
from core.meta import Box
from core.meta import MetaKey
from core.meta import _meta
from core.meta import by_hashtag
from core.meta import by_object_id
from core.meta import by_remote_id
from core.meta import by_type
from core.meta import by_visibility
from core.meta import follow_request_accepted
from core.meta import in_inbox
from core.meta import in_outbox
from core.meta import is_public
from core.meta import not_deleted
from core.meta import not_poll_answer
from core.meta import not_undo
from core.meta import pinned
from core.shared import _build_thread
from core.shared import _get_ip
from core.shared import activitypubify
from core.shared import csrf
from core.shared import htmlify
from core.shared import is_api_request
from core.shared import jsonify
from core.shared import login_required
from core.shared import noindex
from core.shared import paginated_query
from utils.blacklist import is_blacklisted
from utils.emojis import EMOJIS
from utils.highlight import HIGHLIGHT_CSS
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

app.config.update(SESSION_COOKIE_SECURE=True if config.SCHEME == "https" else False)

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


@app.context_processor
def inject_config():
    q = {
        **in_outbox(),
        "$or": [
            {
                **by_type(ActivityType.CREATE),
                **not_deleted(),
                **by_visibility(ap.Visibility.PUBLIC),
            },
            {**by_type(ActivityType.ANNOUNCE), **not_undo()},
        ],
    }
    notes_count = DB.activities.count(q)
    # FIXME(tsileo): rename to all_count, and remove poll answers from it
    all_q = {
        **in_outbox(),
        **by_type([ActivityType.CREATE, ActivityType.ANNOUNCE]),
        **not_deleted(),
        **not_undo(),
        **not_poll_answer(),
    }
    liked_q = {
        **in_outbox(),
        **by_type(ActivityType.LIKE),
        **not_undo(),
        **not_deleted(),
    }
    followers_q = {
        **in_inbox(),
        **by_type(ActivityType.FOLLOW),
        **not_undo(),
        **not_deleted(),
    }
    following_q = {
        **in_outbox(),
        **by_type(ActivityType.FOLLOW),
        **follow_request_accepted(),
        **not_undo(),
        **not_deleted(),
    }
    unread_notifications_q = {_meta(MetaKey.NOTIFICATION_UNREAD): True}

    logged_in = session.get("logged_in", False)

    return dict(
        microblogpub_version=VERSION,
        config=config,
        logged_in=logged_in,
        followers_count=DB.activities.count(followers_q),
        following_count=DB.activities.count(following_q)
        if logged_in or not config.HIDE_FOLLOWING
        else 0,
        notes_count=notes_count,
        liked_count=DB.activities.count(liked_q) if logged_in else 0,
        with_replies_count=DB.activities.count(all_q) if logged_in else 0,
        unread_notifications_count=DB.activities.count(unread_notifications_q)
        if logged_in
        else 0,
        me=ME,
        base_url=config.BASE_URL,
        highlight_css=HIGHLIGHT_CSS,
    )


@app.before_request
def generate_request_id():
    g.request_id = uuid4().hex


@app.after_request
def set_x_powered_by(response):
    response.headers["X-Powered-By"] = "microblog.pub"
    response.headers["X-Request-ID"] = g.request_id
    return response


@app.errorhandler(ValueError)
def handle_value_error(error):
    logger.error(
        f"caught value error for {g.request_id}: {error!r}, {traceback.format_tb(error.__traceback__)}"
    )
    response = jsonify({"message": error.args[0], "request_id": g.request_id})
    response.status_code = 400
    return response


@app.errorhandler(Error)
def handle_activitypub_error(error):
    logger.error(
        f"caught activitypub error for {g.request_id}: {error!r}, {traceback.format_tb(error.__traceback__)}"
    )
    response = jsonify({**error.to_dict(), "request_id": g.request_id})
    response.status_code = error.status_code
    return response


@app.errorhandler(TaskError)
def handle_task_error(error):
    logger.error(
        f"caught activitypub error for {g.request_id}: {error!r}, {traceback.format_tb(error.__traceback__)}"
    )
    response = jsonify({"traceback": error.message, "request_id": g.request_id})
    response.status_code = 500
    return response


@app.errorhandler(InternalServerError)
def handle_500(e):
    tb = "".join(traceback.format_tb(e.__traceback__))
    logger.error(f"caught error {e!r}, {tb}")
    if not session.get("logged_in", False):
        tb = None

    return render_template(
        "error.html", code=500, status_text="Internal Server Error", tb=tb
    )


# @app.errorhandler(Exception)
# def handle_other_error(error):
#    logger.error(
#        f"caught error {error!r}, {traceback.format_tb(error.__traceback__)}"
#    )
#    response = flask_jsonify({})
#    response.status_code = 500
#    return response


def _log_sig():
    sig = request.headers.get("Signature")
    if sig:
        app.logger.info(f"received an authenticated fetch: {sig}")
        try:
            req_verified, actor_id = verify_request(
                request.method, request.path, request.headers, None
            )
            app.logger.info(
                f"authenticated fetch: {req_verified}: {actor_id} {request.headers}"
            )
        except Exception:
            app.logger.exception("failed to verify authenticated fetch")


# App routes

ROBOTS_TXT = """User-agent: *
Disallow: /login
Disallow: /admin/
Disallow: /static/
Disallow: /media/
Disallow: /p/
Disallow: /uploads/"""


@app.route("/robots.txt")
def robots_txt():
    return Response(response=ROBOTS_TXT, headers={"Content-Type": "text/plain"})


@app.route("/microblogpub-0.1.jsonld")
def microblogpub_jsonld():
    """Returns our AP context (embedded in activities @context)."""
    return Response(
        response=json.dumps(jsonld.MICROBLOGPUB),
        headers={"Content-Type": "application/ld+json"},
    )


@app.route("/p/<scheme>/<path:url>")
@noindex
def proxy(scheme: str, url: str) -> Any:
    url = f"{scheme}://{url}"
    req_headers = {
        k: v
        for k, v in dict(request.headers).items()
        if k.lower() not in ["host", "cookie", "", "x-forwarded-for", "x-real-ip"]
        and not k.lower().startswith("broxy-")
    }
    # req_headers["Host"] = urlparse(url).netloc
    resp = requests.get(url, stream=True, headers=req_headers)
    app.logger.info(f"proxied req {url} {req_headers}: {resp!r}")

    def data():
        for chunk in resp.raw.stream(decode_content=False):
            yield chunk

    resp_headers = {
        k: v
        for k, v in dict(resp.raw.headers).items()
        if k.lower()
        in [
            "content-length",
            "content-type",
            "etag",
            "cache-control",
            "expires",
            "date",
            "last-modified",
        ]
    }
    return Response(data(), headers=resp_headers, status=resp.status_code)


@app.route("/media/<media_id>")
@noindex
def serve_media(media_id):
    try:
        f = MEDIA_CACHE.fs.get(ObjectId(media_id))
    except (InvalidId, NoFile):
        abort(404)

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
    try:
        f = MEDIA_CACHE.fs.get(ObjectId(oid))
    except (InvalidId, NoFile):
        abort(404)

    resp = app.response_class(f, direct_passthrough=True, mimetype=f.content_type)
    resp.headers.set("Content-Length", f.length)
    resp.headers.set("ETag", f.md5)
    resp.headers.set(
        "Last-Modified", f.uploadDate.strftime("%a, %d %b %Y %H:%M:%S GMT")
    )
    resp.headers.set("Cache-Control", "public,max-age=31536000,immutable")
    resp.headers.set("Content-Encoding", "gzip")
    return resp


@app.route("/remote_follow", methods=["GET", "POST"])
def remote_follow():
    """Form to allow visitor to perform the remote follow dance."""
    if request.method == "GET":
        return htmlify(render_template("remote_follow.html"))

    csrf.protect()
    profile = request.form.get("profile")
    if not profile.startswith("@"):
        profile = f"@{profile}"
    return redirect(get_remote_follow_template(profile).format(uri=ID))


#######
# Activity pub routes


@app.route("/")
def index():
    if is_api_request():
        _log_sig()
        return activitypubify(**ME)

    q = {
        **in_outbox(),
        "$or": [
            {
                **by_type(ActivityType.CREATE),
                **not_deleted(),
                **by_visibility(ap.Visibility.PUBLIC),
                "$or": [{"meta.pinned": False}, {"meta.pinned": {"$exists": False}}],
            },
            {**by_type(ActivityType.ANNOUNCE), **not_undo()},
        ],
    }

    apinned = []
    # Only fetch the pinned notes if we're on the first page
    if not request.args.get("older_than") and not request.args.get("newer_than"):
        q_pinned = {
            **in_outbox(),
            **by_type(ActivityType.CREATE),
            **not_deleted(),
            **pinned(),
            **by_visibility(ap.Visibility.PUBLIC),
        }
        apinned = list(DB.activities.find(q_pinned))

    outbox_data, older_than, newer_than = paginated_query(
        DB.activities, q, limit=25 - len(apinned)
    )

    return htmlify(
        render_template(
            "index.html",
            outbox_data=outbox_data,
            older_than=older_than,
            newer_than=newer_than,
            pinned=apinned,
        )
    )


@app.route("/all")
@login_required
def all():
    q = {
        **in_outbox(),
        **by_type([ActivityType.CREATE, ActivityType.ANNOUNCE]),
        **not_deleted(),
        **not_undo(),
        **not_poll_answer(),
    }
    outbox_data, older_than, newer_than = paginated_query(DB.activities, q)

    return htmlify(
        render_template(
            "index.html",
            outbox_data=outbox_data,
            older_than=older_than,
            newer_than=newer_than,
        )
    )


@app.route("/note/<note_id>")
def note_by_id(note_id):
    if is_api_request():
        return redirect(url_for("outbox_activity", item_id=note_id))

    query = {}
    # Prevent displaying direct messages on the public frontend
    if not session.get("logged_in", False):
        query = is_public()

    data = DB.activities.find_one(
        {**in_outbox(), **by_remote_id(activity_url(note_id)), **query}
    )
    if not data:
        abort(404)
    if data["meta"].get("deleted", False):
        abort(410)

    thread = _build_thread(data, query=query)
    app.logger.info(f"thread={thread!r}")

    raw_likes = list(
        DB.activities.find(
            {
                **not_undo(),
                **not_deleted(),
                **by_type(ActivityType.LIKE),
                **by_object_id(data["activity"]["object"]["id"]),
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
                **not_undo(),
                **not_deleted(),
                **by_type(ActivityType.ANNOUNCE),
                **by_object_id(data["activity"]["object"]["id"]),
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

    return htmlify(
        render_template(
            "note.html", likes=likes, shares=shares, thread=thread, note=data
        )
    )


@app.route("/outbox", methods=["GET", "POST"])
def outbox():
    if request.method == "GET":
        if not is_api_request():
            abort(404)
        _log_sig()
        # TODO(tsileo): returns the whole outbox if authenticated and look at OCAP support
        q = {
            **in_outbox(),
            "$or": [
                {
                    **by_type(ActivityType.CREATE),
                    **not_deleted(),
                    **by_visibility(ap.Visibility.PUBLIC),
                },
                {**by_type(ActivityType.ANNOUNCE), **not_undo()},
            ],
        }
        return activitypubify(
            **activitypub.build_ordered_collection(
                DB.activities,
                q=q,
                cursor=request.args.get("cursor"),
                map_func=lambda doc: activity_from_doc(doc, embed=True),
                col_name="outbox",
            )
        )

    # Handle POST request aka C2S API
    try:
        _api_required()
    except BadSignature:
        abort(401)

    data = request.get_json(force=True)
    activity = ap.parse_activity(data)
    activity_id = post_to_outbox(activity)

    return Response(status=201, headers={"Location": activity_id})


@app.route("/emoji/<name>")
def ap_emoji(name):
    if name in EMOJIS:
        return activitypubify(**{**EMOJIS[name], "@context": config.DEFAULT_CTX})
    abort(404)


@app.route("/outbox/<item_id>")
def outbox_detail(item_id):
    if "text/html" in request.headers.get("Accept", ""):
        return redirect(url_for("note_by_id", note_id=item_id))

    doc = DB.activities.find_one(
        {
            **in_outbox(),
            **by_remote_id(activity_url(item_id)),
            **not_deleted(),
            **is_public(),
        }
    )
    if not doc:
        abort(404)

    _log_sig()
    if doc["meta"].get("deleted", False):
        abort(404)

    return activitypubify(**activity_from_doc(doc))


@app.route("/outbox/<item_id>/activity")
def outbox_activity(item_id):
    if "text/html" in request.headers.get("Accept", ""):
        return redirect(url_for("note_by_id", note_id=item_id))

    data = find_one_activity(
        {**in_outbox(), **by_remote_id(activity_url(item_id)), **is_public()}
    )
    if not data:
        abort(404)

    _log_sig()
    obj = activity_from_doc(data)
    if data["meta"].get("deleted", False):
        abort(404)

    if obj["type"] != ActivityType.CREATE.value:
        abort(404)
    return activitypubify(**obj["object"])


@app.route("/outbox/<item_id>/replies")
def outbox_activity_replies(item_id):
    if not is_api_request():
        abort(404)
    _log_sig()
    data = DB.activities.find_one(
        {
            **in_outbox(),
            **by_remote_id(activity_url(item_id)),
            **not_deleted(),
            **is_public(),
        }
    )
    if not data:
        abort(404)
    obj = ap.parse_activity(data["activity"])
    if obj.ACTIVITY_TYPE != ActivityType.CREATE:
        abort(404)

    q = {
        **is_public(),
        **not_deleted(),
        **by_type(ActivityType.CREATE),
        "activity.object.inReplyTo": obj.get_object().id,
    }

    return activitypubify(
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
    _log_sig()
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

    return activitypubify(
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
    _log_sig()
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

    return activitypubify(
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

        return activitypubify(
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
            response=json.dumps(
                {
                    "error": "failed to decode request body as JSON",
                    "request_id": g.request_id,
                }
            ),
        )

    # Check the blacklist now to see if we can return super early
    if is_blacklisted(data):
        logger.info(f"dropping activity from blacklisted host: {data['id']}")
        return Response(status=201)

    logger.info(f"request_id={g.request_id} req_headers={request.headers!r}")
    logger.info(f"request_id={g.request_id} raw_data={data}")
    try:
        req_verified, actor_id = verify_request(
            request.method, request.path, request.headers, request.data
        )
        if not req_verified:
            raise Exception("failed to verify request")
        logger.info(f"request_id={g.request_id} signed by {actor_id}")
    except Exception:
        logger.exception(
            f"failed to verify request {g.request_id}, trying to verify the payload by fetching the remote"
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
                                "error": "failed to verify request (using HTTP signatures or fetching the IRI)",
                                "request_id": g.request_id,
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
                        "request_id": g.request_id,
                    },
                }
            )

            return Response(
                status=422,
                headers={"Content-Type": "application/json"},
                response=json.dumps(
                    {
                        "error": "failed to verify request (using HTTP signatures or fetching the IRI)",
                        "request_id": g.request_id,
                    }
                ),
            )

        # We fetched the remote data successfully
        data = remote_data
    try:
        activity = ap.parse_activity(data)
    except ValueError:
        logger.exception("failed to parse activity for req {g.request_id}: {data!r}")

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
                    "request_id": g.request_id,
                },
            }
        )

        return Response(status=201)

    logger.debug(f"inbox activity={g.request_id}/{activity}/{data}")

    post_to_inbox(activity)

    return Response(status=201)


@app.route("/followers")
def followers():
    q = {"box": Box.INBOX.value, "type": ActivityType.FOLLOW.value, "meta.undo": False}

    if is_api_request():
        _log_sig()
        return activitypubify(
            **activitypub.build_ordered_collection(
                DB.activities,
                q=q,
                cursor=request.args.get("cursor"),
                map_func=lambda doc: doc["activity"]["actor"],
                col_name="followers",
            )
        )

    raw_followers, older_than, newer_than = paginated_query(DB.activities, q)
    followers = [doc["meta"] for doc in raw_followers if "actor" in doc.get("meta", {})]
    return htmlify(
        render_template(
            "followers.html",
            followers_data=followers,
            older_than=older_than,
            newer_than=newer_than,
        )
    )


@app.route("/following")
def following():
    q = {
        **in_outbox(),
        **by_type(ActivityType.FOLLOW),
        **not_deleted(),
        **follow_request_accepted(),
        **not_undo(),
    }

    if is_api_request():
        _log_sig()
        if config.HIDE_FOLLOWING:
            return activitypubify(
                **activitypub.simple_build_ordered_collection("following", [])
            )

        return activitypubify(
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
        (doc["remote_id"], doc["meta"])
        for doc in following
        if "remote_id" in doc and "object" in doc.get("meta", {})
    ]
    lists = list(DB.lists.find())
    return htmlify(
        render_template(
            "following.html",
            following_data=following,
            older_than=older_than,
            newer_than=newer_than,
            lists=lists,
        )
    )


@app.route("/tags/<tag>")
def tags(tag):
    if not DB.activities.count(
        {
            **in_outbox(),
            **by_hashtag(tag),
            **by_visibility(ap.Visibility.PUBLIC),
            **not_deleted(),
        }
    ):
        abort(404)
    if not is_api_request():
        return htmlify(
            render_template(
                "tags.html",
                tag=tag,
                outbox_data=DB.activities.find(
                    {
                        **in_outbox(),
                        **by_hashtag(tag),
                        **by_visibility(ap.Visibility.PUBLIC),
                        **not_deleted(),
                    }
                ).sort("meta.published", -1),
            )
        )
    _log_sig()
    q = {
        **in_outbox(),
        **by_hashtag(tag),
        **by_visibility(ap.Visibility.PUBLIC),
        **not_deleted(),
    }
    return activitypubify(
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

    _log_sig()
    q = {
        "box": Box.OUTBOX.value,
        "type": ActivityType.CREATE.value,
        "meta.deleted": False,
        "meta.undo": False,
        "meta.pinned": True,
    }
    data = [clean_activity(doc["activity"]["object"]) for doc in DB.activities.find(q)]
    return activitypubify(
        **activitypub.simple_build_ordered_collection("featured", data)
    )


@app.route("/liked")
@api_required
def liked():
    if not is_api_request():
        q = {
            "box": Box.OUTBOX.value,
            "type": ActivityType.LIKE.value,
            "meta.deleted": False,
            "meta.undo": False,
        }

        liked, older_than, newer_than = paginated_query(DB.activities, q)

        return htmlify(
            render_template(
                "liked.html", liked=liked, older_than=older_than, newer_than=newer_than
            )
        )

    q = {"meta.deleted": False, "meta.undo": False, "type": ActivityType.LIKE.value}
    return activitypubify(
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
