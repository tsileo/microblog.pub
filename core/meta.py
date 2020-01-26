from datetime import datetime
from enum import Enum
from enum import unique
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from little_boxes import activitypub as ap

_SubQuery = Dict[str, Any]


@unique
class Box(Enum):
    INBOX = "inbox"
    OUTBOX = "outbox"
    REPLIES = "replies"


@unique
class FollowStatus(Enum):
    WAITING = "waiting"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@unique
class MetaKey(Enum):
    NOTIFICATION = "notification"
    NOTIFICATION_UNREAD = "notification_unread"
    NOTIFICATION_FOLLOWS_BACK = "notification_follows_back"
    POLL_ANSWER = "poll_answer"
    POLL_ANSWER_TO = "poll_answer_to"
    STREAM = "stream"
    ACTOR_ID = "actor_id"
    ACTOR = "actor"
    ACTOR_HASH = "actor_hash"
    UNDO = "undo"
    PUBLISHED = "published"
    GC_KEEP = "gc_keep"
    OBJECT = "object"
    OBJECT_ID = "object_id"
    OBJECT_ACTOR = "object_actor"
    OBJECT_ACTOR_ID = "object_actor_id"
    OBJECT_ACTOR_HASH = "object_actor_hash"
    PUBLIC = "public"

    PINNED = "pinned"
    HASHTAGS = "hashtags"
    MENTIONS = "mentions"

    FOLLOW_STATUS = "follow_status"

    THREAD_ROOT_PARENT = "thread_root_parent"

    IN_REPLY_TO = "in_reply_to"
    IN_REPLY_TO_SELF = "in_reply_to_self"
    IN_REPLY_TO_ACTOR = "in_reply_to_actor"

    SERVER = "server"
    VISIBILITY = "visibility"
    OBJECT_VISIBILITY = "object_visibility"

    DELETED = "deleted"
    BOOSTED = "boosted"
    LIKED = "liked"

    COUNT_LIKE = "count_like"
    COUNT_BOOST = "count_boost"
    COUNT_REPLY = "count_reply"

    EMOJI_REACTIONS = "emoji_reactions"


def _meta(mk: MetaKey) -> str:
    return f"meta.{mk.value}"


def flag(mk: MetaKey, val: Any) -> _SubQuery:
    return {_meta(mk): val}


def by_remote_id(remote_id: str) -> _SubQuery:
    return {"remote_id": remote_id}


def in_inbox() -> _SubQuery:
    return {"box": Box.INBOX.value}


def in_outbox() -> _SubQuery:
    return {"box": Box.OUTBOX.value}


def by_type(type_: Union[ap.ActivityType, List[ap.ActivityType]]) -> _SubQuery:
    if isinstance(type_, list):
        return {"type": {"$in": [t.value for t in type_]}}

    return {"type": type_.value}


def follow_request_accepted() -> _SubQuery:
    return flag(MetaKey.FOLLOW_STATUS, FollowStatus.ACCEPTED.value)


def not_poll_answer() -> _SubQuery:
    return flag(MetaKey.POLL_ANSWER, False)


def not_in_reply_to() -> _SubQuery:
    return {"activity.object.inReplyTo": None}


def not_undo() -> _SubQuery:
    return flag(MetaKey.UNDO, False)


def not_deleted() -> _SubQuery:
    return flag(MetaKey.DELETED, False)


def pinned() -> _SubQuery:
    return flag(MetaKey.PINNED, True)


def by_actor(actor: ap.BaseActivity) -> _SubQuery:
    return flag(MetaKey.ACTOR_ID, actor.id)


def by_actor_id(actor_id: str) -> _SubQuery:
    return flag(MetaKey.ACTOR_ID, actor_id)


def by_object_id(object_id: str) -> _SubQuery:
    return flag(MetaKey.OBJECT_ID, object_id)


def is_public() -> _SubQuery:
    return flag(MetaKey.PUBLIC, True)


def by_visibility(vis: ap.Visibility) -> _SubQuery:
    return flag(MetaKey.VISIBILITY, vis.name)


def by_object_visibility(vis: ap.Visibility) -> _SubQuery:
    return flag(MetaKey.OBJECT_VISIBILITY, vis.name)


def by_hashtag(ht: str) -> _SubQuery:
    return flag(MetaKey.HASHTAGS, ht)


def inc(mk: MetaKey, val: int) -> _SubQuery:
    return {"$inc": flag(mk, val)}


def upsert(data: Dict[MetaKey, Any]) -> _SubQuery:
    sq: Dict[str, Any] = {}

    for mk, val in data.items():
        sq[_meta(mk)] = val

    return {"$set": sq}


def published_after(dt: datetime) -> _SubQuery:
    return flag(MetaKey.PUBLISHED, {"$gt": ap.format_datetime(dt)})
