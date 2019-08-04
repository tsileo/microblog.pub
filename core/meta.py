from enum import Enum
from enum import unique
from typing import Any
from typing import Dict

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
    UNDO = "undo"
    PUBLISHED = "published"
    GC_KEEP = "gc_keep"
    OBJECT = "object"
    OBJECT_ID = "object_id"
    OBJECT_ACTOR = "object_actor"
    PUBLIC = "public"

    DELETED = "deleted"

    COUNT_LIKE = "count_like"
    COUNT_BOOST = "count_boost"


def _meta(mk: MetaKey) -> str:
    return f"meta.{mk.value}"


def by_remote_id(remote_id: str) -> _SubQuery:
    return {"remote_id": remote_id}


def in_inbox() -> _SubQuery:
    return {"box": Box.INBOX.value}


def in_outbox() -> _SubQuery:
    return {"box": Box.OUTBOX.value}


def by_type(type_: ap.ActivityType) -> _SubQuery:
    return {"type": type_.value}


def not_undo() -> _SubQuery:
    return {_meta(MetaKey.UNDO): False}


def by_actor(actor: ap.BaseActivity) -> _SubQuery:
    return {_meta(MetaKey.ACTOR_ID): actor.id}


def by_object_id(object_id: str) -> _SubQuery:
    return {_meta(MetaKey.OBJECT_ID): object_id}


def is_public() -> _SubQuery:
    return {_meta(MetaKey.PUBLIC): True}


def inc(mk: MetaKey, val: int) -> _SubQuery:
    return {"$inc": {_meta(mk): val}}


def upsert(data: Dict[MetaKey, Any]) -> _SubQuery:
    sq: Dict[str, Any] = {}

    for mk, val in data.items():
        sq[_meta(mk)] = val

    return {"$set": sq}
