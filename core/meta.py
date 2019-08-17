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
class MetaKey(Enum):
    NOTIFICATION = "notification"
    NOTIFICATION_UNREAD = "notification_unread"
    NOTIFICATION_FOLLOWS_BACK = "notification_follows_back"
    POLL_ANSWER = "poll_answer"
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
    THREAD_ROOT_PARENT = "thread_root_parent"

    DELETED = "deleted"
    BOOSTED = "boosted"
    LIKED = "liked"

    COUNT_LIKE = "count_like"
    COUNT_BOOST = "count_boost"
    COUNT_REPLY = "count_reply"


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


def not_undo() -> _SubQuery:
    return flag(MetaKey.UNDO, False)


def not_deleted() -> _SubQuery:
    return flag(MetaKey.DELETED, False)


def by_actor(actor: ap.BaseActivity) -> _SubQuery:
    return flag(MetaKey.ACTOR_ID, actor.id)


def by_object_id(object_id: str) -> _SubQuery:
    return flag(MetaKey.OBJECT_ID, object_id)


def is_public() -> _SubQuery:
    return flag(MetaKey.PUBLIC, True)


def inc(mk: MetaKey, val: int) -> _SubQuery:
    return {"$inc": flag(mk, val)}


def upsert(data: Dict[MetaKey, Any]) -> _SubQuery:
    sq: Dict[str, Any] = {}

    for mk, val in data.items():
        sq[_meta(mk)] = val

    return {"$set": sq}


def published_after(dt: datetime) -> _SubQuery:
    return flag(MetaKey.PUBLISHED, {"$gt": ap.format_datetime(dt)})
