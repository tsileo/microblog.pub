import logging
from functools import singledispatch
from typing import Any
from typing import Dict

from little_boxes import activitypub as ap
from little_boxes.errors import NotAnActivityError

import config
from core.activitypub import _answer_key
from core.activitypub import accept_follow
from core.activitypub import handle_replies
from core.activitypub import update_cached_actor
from core.db import DB
from core.db import update_one_activity
from core.meta import FollowStatus
from core.meta import MetaKey
from core.meta import by_object_id
from core.meta import by_remote_id
from core.meta import by_type
from core.meta import in_inbox
from core.meta import inc
from core.meta import upsert
from core.tasks import Tasks

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
            post_query = {**by_object_id(obj_id), **by_type(ap.ActivityType.CREATE)}
            in_reply_to = ap._get_id(
                DB.activities.find_one(post_query)["activity"]["object"].get(
                    "inReplyTo"
                )
            )
            if in_reply_to:
                DB.activities.update_one(
                    {**by_object_id(in_reply_to), **by_type(ap.ActivityType.CREATE)},
                    inc(MetaKey.COUNT_REPLY, -1),
                )
                DB.replies.update_one(
                    by_remote_id(in_reply_to), inc(MetaKey.COUNT_REPLY, -1)
                )
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
    obj = create.get_object()
    if obj.has_type(ap.ActivityType.QUESTION):
        Tasks.fetch_remote_question(obj)

    Tasks.cache_emojis(obj)

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

    # Process the reply of the announced object if any
    in_reply_to = obj.get_in_reply_to()
    if in_reply_to:
        reply = ap.fetch_remote_activity(in_reply_to)
        if reply.has_type(ap.ActivityType.CREATE):
            reply = reply.get_object()

        in_reply_to_data = {MetaKey.IN_REPLY_TO: in_reply_to}
        # Update the activity to save some data about the reply
        if reply.get_actor().id == obj.get_actor().id:
            in_reply_to_data.update({MetaKey.IN_REPLY_TO_SELF: True})
        else:
            in_reply_to_data.update(
                {MetaKey.IN_REPLY_TO_ACTOR: reply.get_actor().to_dict(embed=True)}
            )
        update_one_activity(by_remote_id(announce.id), upsert(in_reply_to_data))
        # Spawn a task to process it (and determine if it needs to be saved)
        Tasks.process_reply(reply.id)

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
def _emoji_reaction_process_inbox(
    emoji_reaction: ap.EmojiReaction, new_meta: _NewMeta
) -> None:
    _logger.info(f"process_inbox activity={emoji_reaction!r}")
    obj = emoji_reaction.get_object()
    # Try to update an existing emoji reaction counter entry for the activity emoji
    if not update_one_activity(
        {
            **by_type(ap.ActivityType.CREATE),
            **by_object_id(obj.id),
            "meta.emoji_reactions.emoji": emoji_reaction.content,
        },
        {"$inc": {"meta.emoji_reactions.$.count": 1}},
    ):
        # Bootstrap the current emoji counter
        update_one_activity(
            {**by_type(ap.ActivityType.CREATE), **by_object_id(obj.id)},
            {
                "$push": {
                    "meta.emoji_reactions": {
                        "emoji": emoji_reaction.content,
                        "count": 1,
                    }
                }
            },
        )


@process_inbox.register
def _follow_process_inbox(activity: ap.Follow, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={activity!r}")
    # Reply to a Follow with an Accept if we're not manully approving them
    if not config.MANUALLY_APPROVES_FOLLOWERS:
        accept_follow(activity)
    else:
        update_one_activity(
            by_remote_id(activity.id),
            upsert({MetaKey.FOLLOW_STATUS: FollowStatus.WAITING.value}),
        )


def _update_follow_status(follow_id: str, status: FollowStatus) -> None:
    _logger.info(f"{follow_id} is {status}")
    update_one_activity(
        by_remote_id(follow_id), upsert({MetaKey.FOLLOW_STATUS: status.value})
    )


@process_inbox.register
def _accept_process_inbox(activity: ap.Accept, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={activity!r}")
    # Set a flag on the follow
    follow = activity.get_object_id()
    _update_follow_status(follow, FollowStatus.ACCEPTED)


@process_inbox.register
def _reject_process_inbox(activity: ap.Reject, new_meta: _NewMeta) -> None:
    _logger.info(f"process_inbox activity={activity!r}")
    follow = activity.get_object_id()
    _update_follow_status(follow, FollowStatus.REJECTED)


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
