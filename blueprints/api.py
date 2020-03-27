import logging
import mimetypes
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from functools import wraps
from io import BytesIO
from shutil import copyfileobj
from typing import Any
from typing import List

import flask
from bson.objectid import ObjectId
from flask import abort
from flask import current_app as app
from flask import redirect
from flask import request
from flask import session
from itsdangerous import BadSignature
from little_boxes import activitypub as ap
from little_boxes.content_helper import parse_markdown
from little_boxes.errors import ActivityNotFoundError
from little_boxes.errors import NotFromOutboxError
from werkzeug.utils import secure_filename

import config
from config import ADMIN_API_KEY
from config import BASE_URL
from config import DB
from config import DEBUG_MODE
from config import ID
from config import JWT
from config import MEDIA_CACHE
from config import _drop_db
from core import feed
from core.activitypub import accept_follow
from core.activitypub import activity_url
from core.activitypub import new_context
from core.activitypub import post_to_outbox
from core.db import update_one_activity
from core.meta import Box
from core.meta import MetaKey
from core.meta import _meta
from core.meta import by_object_id
from core.meta import by_type
from core.shared import MY_PERSON
from core.shared import _Response
from core.shared import csrf
from core.shared import jsonify
from core.shared import login_required
from core.tasks import Tasks
from utils import emojis
from utils import now

_logger = logging.getLogger(__name__)

blueprint = flask.Blueprint("api", __name__)


def without_id(l):
    out = []
    for d in l:
        if "_id" in d:
            del d["_id"]
        out.append(d)
    return out


def _api_required() -> None:
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
    flask.g.jwt_payload = payload
    app.logger.info(f"api call by {payload}")


def api_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            _api_required()
        except BadSignature:
            abort(401)

        return f(*args, **kwargs)

    return decorated_function


def _user_api_arg(key: str, **kwargs) -> Any:
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


def _user_api_get_note(from_outbox: bool = False) -> ap.BaseActivity:
    oid = _user_api_arg("id")
    app.logger.info(f"fetching {oid}")
    note = ap.parse_activity(ap.get_backend().fetch_iri(oid))
    if from_outbox and not note.id.startswith(ID):
        raise NotFromOutboxError(
            f"cannot load {note.id}, id must be owned by the server"
        )

    return note


def _user_api_response(**kwargs) -> _Response:
    _redirect = _user_api_arg("redirect", default=None)
    if _redirect:
        return redirect(_redirect)

    resp = jsonify(kwargs)
    resp.status_code = 201
    return resp


@blueprint.route("/api/key")
@login_required
def api_user_key() -> _Response:
    return jsonify({"api_key": ADMIN_API_KEY})


@blueprint.route("/note/delete", methods=["POST"])
@api_required
def api_delete() -> _Response:
    """API endpoint to delete a Note activity."""
    note = _user_api_get_note(from_outbox=True)

    # Create the delete, same audience as the Create object
    delete = ap.Delete(
        context=new_context(note),
        actor=ID,
        object=ap.Tombstone(id=note.id).to_dict(embed=True),
        to=note.to,
        cc=note.cc,
        published=now(),
    )

    delete_id = post_to_outbox(delete)

    return _user_api_response(activity=delete_id)


@blueprint.route("/boost", methods=["POST"])
@api_required
def api_boost() -> _Response:
    note = _user_api_get_note()

    # Ensures the note visibility allow us to build an Announce (in respect to the post visibility)
    if ap.get_visibility(note) not in [ap.Visibility.PUBLIC, ap.Visibility.UNLISTED]:
        abort(400)

    announce = ap.Announce(
        actor=MY_PERSON.id,
        object=note.id,
        to=[MY_PERSON.followers, note.attributedTo],
        cc=[ap.AS_PUBLIC],
        published=now(),
        context=new_context(note),
    )
    announce_id = post_to_outbox(announce)

    return _user_api_response(activity=announce_id)


@blueprint.route("/ack_reply", methods=["POST"])
@api_required
def api_ack_reply() -> _Response:
    reply_iri = _user_api_arg("reply_iri")
    obj = ap.fetch_remote_activity(reply_iri)
    if obj.has_type(ap.ActivityType.CREATE):
        obj = obj.get_object()
    # TODO(tsileo): tweak the adressing?
    update_one_activity(
        {**by_type(ap.ActivityType.CREATE), **by_object_id(obj.id)},
        {"$set": {"meta.reply_acked": True}},
    )
    read = ap.Read(
        actor=MY_PERSON.id,
        object=obj.id,
        to=[MY_PERSON.followers],
        cc=[ap.AS_PUBLIC, obj.get_actor().id],
        published=now(),
        context=new_context(obj),
    )

    read_id = post_to_outbox(read)
    return _user_api_response(activity=read_id)


@blueprint.route("/mark_notifications_as_read", methods=["POST"])
@api_required
def api_mark_notification_as_read() -> _Response:
    nid = ObjectId(_user_api_arg("nid"))

    DB.activities.update_many(
        {_meta(MetaKey.NOTIFICATION_UNREAD): True, "_id": {"$lte": nid}},
        {"$set": {_meta(MetaKey.NOTIFICATION_UNREAD): False}},
    )

    return _user_api_response()


@blueprint.route("/vote", methods=["POST"])
@api_required
def api_vote() -> _Response:
    oid = _user_api_arg("id")
    app.logger.info(f"fetching {oid}")
    note = ap.parse_activity(ap.get_backend().fetch_iri(oid))
    choice = _user_api_arg("choice")

    raw_note = dict(
        attributedTo=MY_PERSON.id,
        cc=[],
        to=note.get_actor().id,
        name=choice,
        tag=[],
        context=new_context(note),
        inReplyTo=note.id,
    )
    raw_note["@context"] = config.DEFAULT_CTX

    note = ap.Note(**raw_note)
    create = note.build_create()
    create_id = post_to_outbox(create)

    return _user_api_response(activity=create_id)


@blueprint.route("/like", methods=["POST"])
@api_required
def api_like() -> _Response:
    note = _user_api_get_note()

    to: List[str] = []
    cc: List[str] = []

    note_visibility = ap.get_visibility(note)

    if note_visibility == ap.Visibility.PUBLIC:
        to = [ap.AS_PUBLIC]
        cc = [ID + "/followers", note.get_actor().id]
    elif note_visibility == ap.Visibility.UNLISTED:
        to = [ID + "/followers", note.get_actor().id]
        cc = [ap.AS_PUBLIC]
    else:
        to = [note.get_actor().id]

    like = ap.Like(
        object=note.id,
        actor=MY_PERSON.id,
        to=to,
        cc=cc,
        published=now(),
        context=new_context(note),
    )

    like_id = post_to_outbox(like)

    return _user_api_response(activity=like_id)


@blueprint.route("/bookmark", methods=["POST"])
@api_required
def api_bookmark() -> _Response:
    note = _user_api_get_note()

    undo = _user_api_arg("undo", default=None) == "yes"

    # Try to bookmark the `Create` first
    if not DB.activities.update_one(
        {"activity.object.id": note.id}, {"$set": {"meta.bookmarked": not undo}}
    ).modified_count:
        # Then look for the `Announce`
        DB.activities.update_one(
            {"meta.object.id": note.id}, {"$set": {"meta.bookmarked": not undo}}
        )

    return _user_api_response()


@blueprint.route("/note/pin", methods=["POST"])
@api_required
def api_pin() -> _Response:
    note = _user_api_get_note(from_outbox=True)

    DB.activities.update_one(
        {"activity.object.id": note.id, "box": Box.OUTBOX.value},
        {"$set": {"meta.pinned": True}},
    )

    return _user_api_response(pinned=True)


@blueprint.route("/note/unpin", methods=["POST"])
@api_required
def api_unpin() -> _Response:
    note = _user_api_get_note(from_outbox=True)

    DB.activities.update_one(
        {"activity.object.id": note.id, "box": Box.OUTBOX.value},
        {"$set": {"meta.pinned": False}},
    )

    return _user_api_response(pinned=False)


@blueprint.route("/undo", methods=["POST"])
@api_required
def api_undo() -> _Response:
    oid = _user_api_arg("id")
    doc = DB.activities.find_one(
        {
            "box": Box.OUTBOX.value,
            "$or": [{"remote_id": activity_url(oid)}, {"remote_id": oid}],
        }
    )
    if not doc:
        raise ActivityNotFoundError(f"cannot found {oid}")

    obj = ap.parse_activity(doc.get("activity"))

    undo = ap.Undo(
        actor=MY_PERSON.id,
        context=new_context(obj),
        object=obj.to_dict(embed=True, embed_object_id_only=True),
        published=now(),
        to=obj.to,
        cc=obj.cc,
    )

    # FIXME(tsileo): detect already undo-ed and make this API call idempotent
    undo_id = post_to_outbox(undo)

    return _user_api_response(activity=undo_id)


@blueprint.route("/accept_follow", methods=["POST"])
@api_required
def api_accept_follow() -> _Response:
    oid = _user_api_arg("id")
    doc = DB.activities.find_one({"box": Box.INBOX.value, "remote_id": oid})
    print(doc)
    if not doc:
        raise ActivityNotFoundError(f"cannot found {oid}")

    obj = ap.parse_activity(doc.get("activity"))
    if not obj.has_type(ap.ActivityType.FOLLOW):
        raise ValueError(f"{obj} is not a Follow activity")

    accept_id = accept_follow(obj)

    return _user_api_response(activity=accept_id)


@blueprint.route("/new_list", methods=["POST"])
@api_required
def api_new_list() -> _Response:
    name = _user_api_arg("name")
    if not name:
        raise ValueError("missing name")

    if not DB.lists.find_one({"name": name}):
        DB.lists.insert_one({"name": name, "members": []})

    return _user_api_response(name=name)


@blueprint.route("/delete_list", methods=["POST"])
@api_required
def api_delete_list() -> _Response:
    name = _user_api_arg("name")
    if not name:
        raise ValueError("missing name")

    if not DB.lists.find_one({"name": name}):
        abort(404)

    DB.lists.delete_one({"name": name})

    return _user_api_response()


@blueprint.route("/add_to_list", methods=["POST"])
@api_required
def api_add_to_list() -> _Response:
    list_name = _user_api_arg("list_name")
    if not list_name:
        raise ValueError("missing list_name")

    if not DB.lists.find_one({"name": list_name}):
        raise ValueError(f"list {list_name} does not exist")

    actor_id = _user_api_arg("actor_id")
    if not actor_id:
        raise ValueError("missing actor_id")

    DB.lists.update_one({"name": list_name}, {"$addToSet": {"members": actor_id}})

    return _user_api_response()


@blueprint.route("/remove_from_list", methods=["POST"])
@api_required
def api_remove_from_list() -> _Response:
    list_name = _user_api_arg("list_name")
    if not list_name:
        raise ValueError("missing list_name")

    if not DB.lists.find_one({"name": list_name}):
        raise ValueError(f"list {list_name} does not exist")

    actor_id = _user_api_arg("actor_id")
    if not actor_id:
        raise ValueError("missing actor_id")

    DB.lists.update_one({"name": list_name}, {"$pull": {"members": actor_id}})

    return _user_api_response()


@blueprint.route("/new_note", methods=["POST", "GET"])  # noqa: C901 too complex
@api_required
def api_new_note() -> _Response:
    # Basic Micropub (https://www.w3.org/TR/micropub/) query configuration support
    if request.method == "GET" and request.args.get("q") == "config":
        return jsonify({})
    elif request.method == "GET":
        abort(405)

    source = None
    summary = None
    location = None

    # Basic Micropub (https://www.w3.org/TR/micropub/) "create" support
    is_micropub = False
    # First, check if the Micropub specific fields are present
    if (
        _user_api_arg("h", default=None) == "entry"
        or _user_api_arg("type", default=[None])[0] == "h-entry"
    ):
        is_micropub = True
        # Ensure the "create" scope is set
        if "jwt_payload" not in flask.g or "create" not in flask.g.jwt_payload["scope"]:
            abort(403)

        # Handle location sent via form-data
        # `geo:28.5,9.0,0.0`
        location = _user_api_arg("location", default="")
        if location.startswith("geo:"):
            slat, slng, *_ = location[4:].split(",")
            location = {
                "type": ap.ActivityType.PLACE.value,
                "latitude": float(slat),
                "longitude": float(slng),
            }

        # Handle JSON microformats2 data
        if _user_api_arg("type", default=None):
            _logger.info(f"Micropub request: {request.json}")
            try:
                source = request.json["properties"]["content"][0]
            except (ValueError, KeyError):
                pass

            # Handle HTML
            if isinstance(source, dict):
                source = source.get("html")

            try:
                summary = request.json["properties"]["name"][0]
            except (ValueError, KeyError):
                pass

        # Try to parse the name as summary if the payload is POSTed using form-data
        if summary is None:
            summary = _user_api_arg("name", default=None)

    # This step will also parse content from Micropub request
    if source is None:
        source = _user_api_arg("content", default=None)

    if not source:
        raise ValueError("missing content")

    if summary is None:
        summary = _user_api_arg("summary", default="")

    if not location:
        if _user_api_arg("location_lat", default=None):
            lat = float(_user_api_arg("location_lat"))
            lng = float(_user_api_arg("location_lng"))
            loc_name = _user_api_arg("location_name", default="")
            location = {
                "type": ap.ActivityType.PLACE.value,
                "name": loc_name,
                "latitude": lat,
                "longitude": lng,
            }

    # All the following fields are specific to the API (i.e. not Micropub related)
    _reply, reply = None, None
    try:
        _reply = _user_api_arg("reply")
    except ValueError:
        pass

    visibility = ap.Visibility[
        _user_api_arg("visibility", default=ap.Visibility.PUBLIC.name)
    ]

    content, tags = parse_markdown(source)

    # Check for custom emojis
    tags = tags + emojis.tags(content)

    to: List[str] = []
    cc: List[str] = []

    if visibility == ap.Visibility.PUBLIC:
        to = [ap.AS_PUBLIC]
        cc = [ID + "/followers"]
    elif visibility == ap.Visibility.UNLISTED:
        to = [ID + "/followers"]
        cc = [ap.AS_PUBLIC]
    elif visibility == ap.Visibility.FOLLOWERS_ONLY:
        to = [ID + "/followers"]
        cc = []

    if _reply:
        reply = ap.fetch_remote_activity(_reply)
        if visibility == ap.Visibility.DIRECT:
            to.append(reply.attributedTo)
        else:
            cc.append(reply.attributedTo)

    context = new_context(reply)

    for tag in tags:
        if tag["type"] == "Mention":
            to.append(tag["href"])

    raw_note = dict(
        attributedTo=MY_PERSON.id,
        cc=list(set(cc) - set([MY_PERSON.id])),
        to=list(set(to) - set([MY_PERSON.id])),
        summary=summary,
        content=content,
        tag=tags,
        source={"mediaType": "text/markdown", "content": source},
        inReplyTo=reply.id if reply else None,
        context=context,
    )

    if location:
        raw_note["location"] = location

    if request.files:
        for f in request.files.keys():
            if not request.files[f].filename:
                continue

            file = request.files[f]
            rfilename = secure_filename(file.filename)
            with BytesIO() as buf:
                # bypass file.save(), because it can't save to a file-like object
                copyfileobj(file.stream, buf, 16384)
                oid = MEDIA_CACHE.save_upload(buf, rfilename)
            mtype = mimetypes.guess_type(rfilename)[0]

            raw_note["attachment"] = [
                {
                    "mediaType": mtype,
                    "name": _user_api_arg("file_description", default=rfilename),
                    "type": "Document",
                    "url": f"{BASE_URL}/uploads/{oid}/{rfilename}",
                }
            ]

    note = ap.Note(**raw_note)
    create = note.build_create()
    create_id = post_to_outbox(create)

    # Return a 201 with the note URL in the Location header if this was a Micropub request
    if is_micropub:
        resp = flask.Response("", headers={"Location": create_id})
        resp.status_code = 201
        return resp

    return _user_api_response(activity=create_id)


@blueprint.route("/new_question", methods=["POST"])
@api_required
def api_new_question() -> _Response:
    source = _user_api_arg("content")
    if not source:
        raise ValueError("missing content")

    content, tags = parse_markdown(source)
    tags = tags + emojis.tags(content)

    cc = [ID + "/followers"]

    for tag in tags:
        if tag["type"] == "Mention":
            cc.append(tag["href"])

    answers = []
    for i in range(4):
        a = _user_api_arg(f"answer{i}", default=None)
        if not a:
            break
        answers.append(
            {
                "type": ap.ActivityType.NOTE.value,
                "name": a,
                "replies": {"type": ap.ActivityType.COLLECTION.value, "totalItems": 0},
            }
        )

    open_for = int(_user_api_arg("open_for"))
    choices = {
        "endTime": ap.format_datetime(
            datetime.now(timezone.utc) + timedelta(minutes=open_for)
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
        context=new_context(),
        content=content,
        tag=tags,
        source={"mediaType": "text/markdown", "content": source},
        inReplyTo=None,
        **choices,
    )

    question = ap.Question(**raw_question)
    create = question.build_create()
    create_id = post_to_outbox(create)

    Tasks.update_question_outbox(create_id, open_for)

    return _user_api_response(activity=create_id)


@blueprint.route("/block", methods=["POST"])
@api_required
def api_block() -> _Response:
    actor = _user_api_arg("actor")

    existing = DB.activities.find_one(
        {
            "box": Box.OUTBOX.value,
            "type": ap.ActivityType.BLOCK.value,
            "activity.object": actor,
            "meta.undo": False,
        }
    )
    if existing:
        return _user_api_response(activity=existing["activity"]["id"])

    block = ap.Block(actor=MY_PERSON.id, object=actor)
    block_id = post_to_outbox(block)

    return _user_api_response(activity=block_id)


@blueprint.route("/follow", methods=["POST"])
@api_required
def api_follow() -> _Response:
    actor = _user_api_arg("actor")

    q = {
        "box": Box.OUTBOX.value,
        "type": ap.ActivityType.FOLLOW.value,
        "meta.undo": False,
        "activity.object": actor,
    }

    existing = DB.activities.find_one(q)
    if existing:
        return _user_api_response(activity=existing["activity"]["id"])

    follow = ap.Follow(
        actor=MY_PERSON.id,
        object=actor,
        to=[actor],
        cc=[ap.AS_PUBLIC],
        published=now(),
        context=new_context(),
    )
    follow_id = post_to_outbox(follow)

    return _user_api_response(activity=follow_id)


@blueprint.route("/debug", methods=["GET", "DELETE"])
@api_required
def api_debug() -> _Response:
    """Endpoint used/needed for testing, only works in DEBUG_MODE."""
    if not DEBUG_MODE:
        return jsonify({"message": "DEBUG_MODE is off"})

    if request.method == "DELETE":
        _drop_db()
        return jsonify(dict(message="DB dropped"))

    return jsonify(
        dict(
            inbox=DB.activities.count({"box": Box.INBOX.value}),
            outbox=DB.activities.count({"box": Box.OUTBOX.value}),
            outbox_data=without_id(DB.activities.find({"box": Box.OUTBOX.value})),
        )
    )


@blueprint.route("/stream")
@api_required
def api_stream() -> _Response:
    return jsonify(
        feed.build_inbox_json_feed("/api/stream", request.args.get("cursor"))
    )
