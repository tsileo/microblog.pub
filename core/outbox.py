import logging
from datetime import datetime
from functools import singledispatch
from typing import Any
from typing import Dict

from little_boxes import activitypub as ap

from core.activitypub import handle_replies
from core.db import find_one_activity
from core.db import update_many_activities
from core.db import update_one_activity
from core.meta import MetaKey
from core.meta import by_object_id
from core.meta import by_type
from core.meta import inc
from core.meta import upsert
from core.tasks import Tasks

_logger = logging.getLogger(__name__)

_NewMeta = Dict[str, Any]


@singledispatch
def process_outbox(activity: ap.BaseActivity, new_meta: _NewMeta) -> None:
    _logger.warning(f"skipping {activity!r}")
    return None


@process_outbox.register
def _delete_process_outbox(delete: ap.Delete, new_meta: _NewMeta) -> None:
    _logger.info(f"process_outbox activity={delete!r}")
    obj_id = delete.get_object_id()

    # Flag everything referencing the deleted object as deleted (except the Delete activity itself)
    update_many_activities(
        {**by_object_id(obj_id), "remote_id": {"$ne": delete.id}},
        upsert({MetaKey.DELETED: True, MetaKey.UNDO: True}),
    )

    # If the deleted activity was in DB, decrease some threads-related counter
    data = find_one_activity(
        {**by_object_id(obj_id), **by_type(ap.ActivityType.CREATE)}
    )
    _logger.info(f"found local copy of deleted activity: {data}")
    if data:
        obj = ap.parse_activity(data["activity"]).get_object()
        _logger.info(f"obj={obj!r}")
        in_reply_to = obj.get_in_reply_to()
        if in_reply_to:
            update_one_activity(
                {**by_type(ap.ActivityType.CREATE), **by_object_id(in_reply_to)},
                {"$inc": {"meta.count_reply": -1, "meta.count_direct_reply": -1}},
            )


@process_outbox.register
def _update_process_outbox(update: ap.Update, new_meta: _NewMeta) -> None:
    _logger.info(f"process_outbox activity={update!r}")

    obj = update._data["object"]

    update_prefix = "activity.object."
    to_update: Dict[str, Any] = {"$set": dict(), "$unset": dict()}
    to_update["$set"][f"{update_prefix}updated"] = (
        datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    )
    for k, v in obj.items():
        if k in ["id", "type"]:
            continue
        if v is None:
            to_update["$unset"][f"{update_prefix}{k}"] = ""
        else:
            to_update["$set"][f"{update_prefix}{k}"] = v

    if len(to_update["$unset"]) == 0:
        del to_update["$unset"]

    _logger.info(f"updating note from outbox {obj!r} {to_update}")
    update_one_activity({"activity.object.id": obj["id"]}, to_update)
    # FIXME(tsileo): should send an Update (but not a partial one, to all the note's recipients
    # (create a new Update with the result of the update, and send it without saving it?)


@process_outbox.register
def _create_process_outbox(create: ap.Create, new_meta: _NewMeta) -> None:
    _logger.info(f"process_outbox activity={create!r}")
    handle_replies(create)


@process_outbox.register
def _announce_process_outbox(announce: ap.Announce, new_meta: _NewMeta) -> None:
    _logger.info(f"process_outbox activity={announce!r}")

    obj = announce.get_object()
    if obj.has_type(ap.ActivityType.QUESTION):
        Tasks.fetch_remote_question(obj)

    Tasks.cache_object(announce.id)

    update_one_activity(
        {**by_object_id(obj.id), **by_type(ap.ActivityType.CREATE)},
        upsert({MetaKey.BOOSTED: announce.id}),
    )


@process_outbox.register
def _like_process_outbox(like: ap.Like, new_meta: _NewMeta) -> None:
    _logger.info(f"process_outbox activity={like!r}")

    obj = like.get_object()
    if obj.has_type(ap.ActivityType.QUESTION):
        Tasks.fetch_remote_question(obj)

    # Cache the object for display on the "Liked" public page
    Tasks.cache_object(like.id)

    update_one_activity(
        {**by_object_id(obj.id), **by_type(ap.ActivityType.CREATE)},
        {**inc(MetaKey.COUNT_LIKE, 1), **upsert({MetaKey.LIKED: like.id})},
    )


@process_outbox.register
def _undo_process_outbox(undo: ap.Undo, new_meta: _NewMeta) -> None:
    _logger.info(f"process_outbox activity={undo!r}")
    obj = undo.get_object()
    update_one_activity({"remote_id": obj.id}, {"$set": {"meta.undo": True}})

    # Undo Like
    if obj.has_type(ap.ActivityType.LIKE):
        liked = obj.get_object_id()
        update_one_activity(
            {**by_object_id(liked), **by_type(ap.ActivityType.CREATE)},
            {**inc(MetaKey.COUNT_LIKE, -1), **upsert({MetaKey.LIKED: False})},
        )

    elif obj.has_type(ap.ActivityType.ANNOUNCE):
        announced = obj.get_object_id()
        update_one_activity(
            {**by_object_id(announced), **by_type(ap.ActivityType.CREATE)},
            upsert({MetaKey.BOOSTED: False}),
        )

    # Undo Follow (undo new following)
    elif obj.has_type(ap.ActivityType.FOLLOW):
        pass
        # do nothing
