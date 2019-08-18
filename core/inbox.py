import logging
from functools import singledispatch
from typing import Any
from typing import Dict

from little_boxes import activitypub as ap
from little_boxes.errors import NotAnActivityError

import config
from core.activitypub import _answer_key
from core.activitypub import handle_replies
from core.activitypub import post_to_outbox
from core.activitypub import update_cached_actor
from core.db import DB
from core.db import update_one_activity
from core.meta import MetaKey
from core.meta import by_object_id
from core.meta import by_remote_id
from core.meta import by_type
from core.meta import in_inbox
from core.meta import inc
from core.meta import upsert
from core.shared import MY_PERSON
from core.shared import back
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
    _logger.debug(f"delete object={obj_id}")
    try:
        # FIXME(tsileo): call the DB here instead? like for the oubox
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

    update_one_activity(
        {**by_object_id(obj_id), **by_type(ap.ActivityType.CREATE)},
        upsert({MetaKey.DELETED: True}),
    )

    # Foce undo other related activities
    DB.activities.update(by_object_id(obj_id), upsert({MetaKey.UNDO: True}))


@process_inbox.register
def _update_process_inbox(update: ap.Update, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={update!r}")
    obj = update.get_object()
    if obj.ACTIVITY_TYPE == ap.ActivityType.NOTE:
        update_one_activity(
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

        update_one_activity({**in_inbox(), **by_object_id(obj.id)}, {"$set": _set})
        # Also update the cached copies of the question (like Announce and Like)
        DB.activities.update_many(
            by_object_id(obj.id), upsert({MetaKey.OBJECT: obj.to_dict()})
        )

    elif obj.has_type(ap.ACTOR_TYPES):
        actor = ap.fetch_remote_activity(obj.id, no_cache=True)
        update_cached_actor(actor)

    else:
        raise ValueError(f"don't know how to update {obj!r}")


@process_inbox.register
def _create_process_inbox(create: ap.Create, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={create!r}")
    # If it's a `Quesiion`, trigger an async task for updating it later (by fetching the remote and updating the
    # local copy)
    question = create.get_object()
    if question.has_type(ap.ActivityType.QUESTION):
        Tasks.fetch_remote_question(question)

    handle_replies(create)


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

    # Cache the announced object
    Tasks.cache_object(announce.id)

    update_one_activity(
        {**by_type(ap.ActivityType.CREATE), **by_object_id(obj.id)},
        inc(MetaKey.COUNT_BOOST, 1),
    )


@process_inbox.register
def _like_process_inbox(like: ap.Like, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={like!r}")
    obj = like.get_object()
    # Update the meta counter if the object is published by the server
    update_one_activity(
        {**by_type(ap.ActivityType.CREATE), **by_object_id(obj.id)},
        inc(MetaKey.COUNT_LIKE, 1),
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
    # Fetch the object that's been undo'ed
    obj = activity.get_object()

    # Set the undo flag on the mentionned activity
    update_one_activity(by_remote_id(obj.id), upsert({MetaKey.UNDO: True}))

    # Handle cached counters
    if obj.has_type(ap.ActivityType.LIKE):
        # Update the meta counter if the object is published by the server
        update_one_activity(
            {**by_object_id(obj.get_object_id()), **by_type(ap.ActivityType.CREATE)},
            inc(MetaKey.COUNT_LIKE, -1),
        )
    elif obj.has_type(ap.ActivityType.ANNOUNCE):
        announced = obj.get_object()
        # Update the meta counter if the object is published by the server
        update_one_activity(
            {**by_type(ap.ActivityType.CREATE), **by_object_id(announced.id)},
            inc(MetaKey.COUNT_BOOST, -1),
        )
