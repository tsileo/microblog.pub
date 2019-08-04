import json
import mimetypes
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from functools import wraps
from io import BytesIO
from typing import Any
from typing import List

import flask
from bson.objectid import ObjectId
from flask import Response
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
from core import activitypub
from core.activitypub import activity_url
from core.activitypub import post_to_outbox
from core.meta import Box
from core.meta import MetaKey
from core.meta import _meta
from core.shared import MY_PERSON
from core.shared import _Response
from core.shared import csrf
from core.shared import login_required
from core.tasks import Tasks
from utils import now

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

    resp = flask.jsonify(**kwargs)
    resp.status_code = 201
    return resp


@blueprint.route("/api/key")
@login_required
def api_user_key() -> _Response:
    return flask.jsonify(api_key=ADMIN_API_KEY)


@blueprint.route("/note/delete", methods=["POST"])
@api_required
def api_delete() -> _Response:
    """API endpoint to delete a Note activity."""
    note = _user_api_get_note(from_outbox=True)

    # Create the delete, same audience as the Create object
    delete = ap.Delete(
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
    )
    announce_id = post_to_outbox(announce)

    return _user_api_response(activity=announce_id)


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

    like = ap.Like(object=note.id, actor=MY_PERSON.id, to=to, cc=cc, published=now())

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
        object=obj.to_dict(embed=True, embed_object_id_only=True),
        published=now(),
        to=obj.to,
        cc=obj.cc,
    )

    # FIXME(tsileo): detect already undo-ed and make this API call idempotent
    undo_id = post_to_outbox(undo)

    return _user_api_response(activity=undo_id)


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


@blueprint.route("/new_note", methods=["POST"])
@api_required
def api_new_note() -> _Response:
    source = _user_api_arg("content")
    if not source:
        raise ValueError("missing content")

    _reply, reply = None, None
    try:
        _reply = _user_api_arg("reply")
    except ValueError:
        pass

    visibility = ap.Visibility[
        _user_api_arg("visibility", default=ap.Visibility.PUBLIC.name)
    ]

    content, tags = parse_markdown(source)

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

    for tag in tags:
        if tag["type"] == "Mention":
            if visibility == ap.Visibility.DIRECT:
                to.append(tag["href"])
            else:
                cc.append(tag["href"])

    raw_note = dict(
        attributedTo=MY_PERSON.id,
        cc=list(set(cc)),
        to=list(set(to)),
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


@blueprint.route("/new_question", methods=["POST"])
@api_required
def api_new_question() -> _Response:
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
        actor=MY_PERSON.id, object=actor, to=[actor], cc=[ap.AS_PUBLIC], published=now()
    )
    follow_id = post_to_outbox(follow)

    return _user_api_response(activity=follow_id)


@blueprint.route("/debug", methods=["GET", "DELETE"])
@api_required
def api_debug() -> _Response:
    """Endpoint used/needed for testing, only works in DEBUG_MODE."""
    if not DEBUG_MODE:
        return flask.jsonify(message="DEBUG_MODE is off")

    if request.method == "DELETE":
        _drop_db()
        return flask.jsonify(message="DB dropped")

    return flask.jsonify(
        inbox=DB.activities.count({"box": Box.INBOX.value}),
        outbox=DB.activities.count({"box": Box.OUTBOX.value}),
        outbox_data=without_id(DB.activities.find({"box": Box.OUTBOX.value})),
    )


@blueprint.route("/stream")
@api_required
def api_stream() -> _Response:
    return Response(
        response=json.dumps(
            activitypub.build_inbox_json_feed("/api/stream", request.args.get("cursor"))
        ),
        headers={"Content-Type": "application/json"},
    )
