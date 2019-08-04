import logging
from functools import singledispatch
from typing import Any
from typing import Dict

from little_boxes import activitypub as ap
from little_boxes.errors import NotAnActivityError

import config
from core.activitypub import _answer_key
from core.db import DB
from core.meta import Box
from core.shared import MY_PERSON
from core.shared import back
from core.shared import post_to_outbox
from core.tasks import Tasks
from utils import now

_logger = logging.getLogger(__name__)

_NewMeta = Dict[str, Any]


@singledispatch
def process_inbox(activity: ap.BaseActivity, new_meta: _NewMeta) -> None:
    _logger.warning(f"skipping {activity!r}")
    return None


@process_inbox.register
def _delete_process_inbox(delete: ap.Delete, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={delete!r}")
    obj_id = delete.get_object_id()
    _logger.debug("delete object={obj_id}")
    try:
        obj = ap.fetch_remote_activity(obj_id)
        _logger.info(f"inbox_delete handle_replies obj={obj!r}")
        in_reply_to = obj.get_in_reply_to() if obj.inReplyTo else None
        if obj.has_type(ap.CREATE_TYPES):
            in_reply_to = ap._get_id(
                DB.activities.find_one(
                    {"meta.object_id": obj_id, "type": ap.ActivityType.CREATE.value}
                )["activity"]["object"].get("inReplyTo")
            )
            if in_reply_to:
                back._handle_replies_delete(MY_PERSON, in_reply_to)
    except Exception:
        _logger.exception(f"failed to handle delete replies for {obj_id}")

    DB.activities.update_one(
        {"meta.object_id": obj_id, "type": "Create"}, {"$set": {"meta.deleted": True}}
    )

    # Foce undo other related activities
    DB.activities.update({"meta.object_id": obj_id}, {"$set": {"meta.undo": True}})


@process_inbox.register
def _update_process_inbox(update: ap.Update, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={update!r}")
    obj = update.get_object()
    if obj.ACTIVITY_TYPE == ap.ActivityType.NOTE:
        DB.activities.update_one(
            {"activity.object.id": obj.id}, {"$set": {"activity.object": obj.to_dict()}}
        )
    elif obj.has_type(ap.ActivityType.QUESTION):
        choices = obj._data.get("oneOf", obj.anyOf)
        total_replies = 0
        _set = {}
        for choice in choices:
            answer_key = _answer_key(choice["name"])
            cnt = choice["replies"]["totalItems"]
            total_replies += cnt
            _set[f"meta.question_answers.{answer_key}"] = cnt

        _set["meta.question_replies"] = total_replies

        DB.activities.update_one(
            {"box": Box.INBOX.value, "activity.object.id": obj.id}, {"$set": _set}
        )
        # Also update the cached copies of the question (like Announce and Like)
        DB.activities.update_many(
            {"meta.object.id": obj.id}, {"$set": {"meta.object": obj.to_dict()}}
        )

    # FIXME(tsileo): handle update actor amd inbox_update_note/inbox_update_actor


@process_inbox.register
def _create_process_inbox(create: ap.Create, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={create!r}")
    # If it's a `Quesiion`, trigger an async task for updating it later (by fetching the remote and updating the
    # local copy)
    question = create.get_object()
    if question.has_type(ap.ActivityType.QUESTION):
        Tasks.fetch_remote_question(question)

    back._handle_replies(MY_PERSON, create)


@process_inbox.register
def _announce_process_inbox(announce: ap.Announce, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={announce!r}")
    # TODO(tsileo): actually drop it without storing it and better logging, also move the check somewhere else
    # or remove it?
    try:
        obj = announce.get_object()
    except NotAnActivityError:
        _logger.exception(
            f'received an Annouce referencing an OStatus notice ({announce._data["object"]}), dropping the message'
        )
        return

    if obj.has_type(ap.ActivityType.QUESTION):
        Tasks.fetch_remote_question(obj)

    DB.activities.update_one(
        {"remote_id": announce.id},
        {
            "$set": {
                "meta.object": obj.to_dict(embed=True),
                "meta.object_actor": obj.get_actor(embed=True),
            }
        },
    )
    DB.activities.update_one(
        {"activity.object.id": obj.id}, {"$inc": {"meta.count_boost": 1}}
    )


@process_inbox.register
def _like_process_inbox(like: ap.Like, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={like!r}")
    obj = like.get_object()
    # Update the meta counter if the object is published by the server
    DB.activities.update_one(
        {"box": Box.OUTBOX.value, "activity.object.id": obj.id},
        {"$inc": {"meta.count_like": 1}},
    )


@process_inbox.register
def _follow_process_inbox(activity: ap.Follow, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={activity!r}")
    # Reply to a Follow with an Accept
    actor_id = activity.get_actor().id
    accept = ap.Accept(
        actor=config.ID,
        object={
            "type": "Follow",
            "id": activity.id,
            "object": activity.get_object_id(),
            "actor": actor_id,
        },
        to=[actor_id],
        published=now(),
    )
    post_to_outbox(accept)


@process_inbox.register
def _undo_process_inbox(activity: ap.Undo, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={activity!r}")
    obj = activity.get_object()
    if obj.has_type(ap.ActivityType.LIKE):
        liked = activity.get_object()
        # Update the meta counter if the object is published by the server
        DB.activities.update_one(
            {"box": Box.OUTBOX.value, "activity.object.id": liked.id},
            {"$inc": {"meta.count_like": -1}},
        )
        DB.activities.update_one({"remote_id": obj.id}, {"$set": {"meta.undo": True}})
    elif obj.has_type(ap.ActivityType.ANNOUNCE):
        announced = obj.get_object()
        # Update the meta counter if the object is published by the server
        DB.activities.update_one(
            {"activity.object.id": announced.id}, {"$inc": {"meta.count_boost": -1}}
        )
        DB.activities.update_one({"remote_id": obj.id}, {"$set": {"meta.undo": True}})
    elif obj.has_type(ap.ActivityType.FOLLOW):
        DB.activities.update_one({"remote_id": obj.id}, {"$set": {"meta.undo": True}})
