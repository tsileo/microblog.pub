import logging
from functools import singledispatch
from typing import Any
from typing import Dict

from little_boxes import activitypub as ap

from config import BASE_URL
from config import DB
from config import MetaKey
from config import _meta
from tasks import Tasks
from utils.meta import by_actor
from utils.meta import by_type
from utils.meta import in_inbox
from utils.meta import not_undo

_logger = logging.getLogger(__name__)

_NewMeta = Dict[str, Any]


def _is_from_outbox(activity: ap.BaseActivity) -> bool:
    return activity.id.startswith(BASE_URL)


def _flag_as_notification(activity: ap.BaseActivity, new_meta: _NewMeta) -> None:
    new_meta.update(
        **{_meta(MetaKey.NOTIFICATION): True, _meta(MetaKey.NOTIFICATION_UNREAD): True}
    )
    return None


@singledispatch
def set_inbox_flags(activity: ap.BaseActivity, new_meta: _NewMeta) -> None:
    return None


@set_inbox_flags.register
def _accept_set_inbox_flags(activity: ap.Accept, new_meta: _NewMeta) -> None:
    """Handle notifications for "accepted" following requests."""
    _logger.info(f"set_inbox_flags activity={activity!r}")
    # Check if this actor already follow us back
    follows_back = False
    follow_query = {
        **in_inbox(),
        **by_type(ap.ActivityType.FOLLOW),
        **by_actor(activity.get_actor()),
        **not_undo(),
    }
    raw_follow = DB.activities.find_one(follow_query)
    if raw_follow:
        follows_back = True

        DB.activities.update_many(
            follow_query, {"$set": {_meta(MetaKey.NOTIFICATION_FOLLOWS_BACK): True}}
        )

    # This Accept will be a "You started following $actor" notification
    _flag_as_notification(activity, new_meta)
    new_meta.update(**{_meta(MetaKey.NOTIFICATION_FOLLOWS_BACK): follows_back})
    return None


@set_inbox_flags.register
def _follow_set_inbox_flags(activity: ap.Follow, new_meta: _NewMeta) -> None:
    """Handle notification for new followers."""
    # Check if we're already following this actor
    follows_back = False
    accept_query = {
        **in_inbox(),
        **by_type(ap.ActivityType.ACCEPT),
        **by_actor(activity.get_actor()),
        **not_undo(),
    }
    raw_accept = DB.activities.find_one(accept_query)
    if raw_accept:
        follows_back = True

        DB.activities.update_many(
            accept_query, {"$set": {_meta(MetaKey.NOTIFICATION_FOLLOWS_BACK): True}}
        )

    # This Follow will be a "$actor started following you" notification
    _flag_as_notification(activity, new_meta)
    new_meta.update(**{_meta(MetaKey.NOTIFICATION_FOLLOWS_BACK): follows_back})
    return None


@set_inbox_flags.register
def _like_set_inbox_flags(activity: ap.Like, new_meta: _NewMeta) -> None:
    # Is it a Like of local acitivty/from the outbox
    if _is_from_outbox(activity.get_object()):
        # Flag it as a notification
        _flag_as_notification(activity, new_meta)

        # Cache the object (for display on the notifcation page)
        Tasks.cache_object(activity.id)

        # Also set the "keep mark" for the GC (as we want to keep it forever)
        new_meta.update(**{_meta(MetaKey.GC_KEEP): True})

    return None


@set_inbox_flags.register
def _announce_set_inbox_flags(activity: ap.Announce, new_meta: _NewMeta) -> None:
    # Is it a Like of local acitivty/from the outbox
    if _is_from_outbox(activity.get_object()):
        # Flag it as a notification
        _flag_as_notification(activity, new_meta)

        # Also set the "keep mark" for the GC (as we want to keep it forever)
        new_meta.update(**{_meta(MetaKey.GC_KEEP): True})

    # Cache the object in all case (for display on the notifcation page)
    Tasks.cache_object(activity.id)

    return None
