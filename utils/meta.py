from enum import Enum
from typing import Any
from typing import Dict

from little_boxes import activitypub as ap

_SubQuery = Dict[str, Any]


class Box(Enum):
    INBOX = "inbox"
    OUTBOX = "outbox"
    REPLIES = "replies"


class MetaKey(Enum):
    NOTIFICATION = "notification"
    NOTIFICATION_UNREAD = "notification_unread"
    NOTIFICATION_FOLLOWS_BACK = "notification_follows_back"
    ACTOR_ID = "actor_id"
    UNDO = "undo"
    PUBLISHED = "published"


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
