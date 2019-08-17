import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from functools import singledispatch
from typing import Any
from typing import Dict
from urllib.parse import urlparse

from little_boxes import activitypub as ap

from config import BASE_URL
from config import DB
from core.db import find_one_activity
from core.meta import MetaKey
from core.meta import _meta
from core.meta import by_actor
from core.meta import by_object_id
from core.meta import by_type
from core.meta import flag
from core.meta import in_inbox
from core.meta import not_undo
from core.meta import published_after
from core.tasks import Tasks

_logger = logging.getLogger(__name__)

_NewMeta = Dict[str, Any]

_LOCAL_NETLOC = urlparse(BASE_URL).netloc


def _is_from_outbox(activity: ap.BaseActivity) -> bool:
    return activity.id.startswith(BASE_URL)


def _is_local(url: str) -> bool:
    return urlparse(url).netloc == _LOCAL_NETLOC


def _flag_as_notification(activity: ap.BaseActivity, new_meta: _NewMeta) -> None:
    new_meta.update(
        {_meta(MetaKey.NOTIFICATION): True, _meta(MetaKey.NOTIFICATION_UNREAD): True}
    )
    return None


def _set_flag(meta: _NewMeta, meta_key: MetaKey, value: Any = True) -> None:
    meta.update({_meta(meta_key): value})
    return None


@singledispatch
def set_inbox_flags(activity: ap.BaseActivity, new_meta: _NewMeta) -> None:
    _logger.warning(f"skipping {activity!r}")
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
    _set_flag(new_meta, MetaKey.GC_KEEP)
    _set_flag(new_meta, MetaKey.NOTIFICATION_FOLLOWS_BACK, follows_back)
    return None


@set_inbox_flags.register
def _follow_set_inbox_flags(activity: ap.Follow, new_meta: _NewMeta) -> None:
    """Handle notification for new followers."""
    _logger.info(f"set_inbox_flags activity={activity!r}")
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
    _set_flag(new_meta, MetaKey.GC_KEEP)
    _set_flag(new_meta, MetaKey.NOTIFICATION_FOLLOWS_BACK, follows_back)
    return None


@set_inbox_flags.register
def _like_set_inbox_flags(activity: ap.Like, new_meta: _NewMeta) -> None:
    _logger.info(f"set_inbox_flags activity={activity!r}")
    # Is it a Like of local acitivty/from the outbox
    if _is_from_outbox(activity.get_object()):
        # Flag it as a notification
        _flag_as_notification(activity, new_meta)

        # Cache the object (for display on the notifcation page)
        Tasks.cache_object(activity.id)

        # Also set the "keep mark" for the GC (as we want to keep it forever)
        _set_flag(new_meta, MetaKey.GC_KEEP)

    return None


@set_inbox_flags.register
def _announce_set_inbox_flags(activity: ap.Announce, new_meta: _NewMeta) -> None:
    _logger.info(f"set_inbox_flags activity={activity!r}")
    obj = activity.get_object()
    # Is it a Annnounce/boost of local acitivty/from the outbox
    if _is_from_outbox(obj):
        # Flag it as a notification
        _flag_as_notification(activity, new_meta)

        # Also set the "keep mark" for the GC (as we want to keep it forever)
        _set_flag(new_meta, MetaKey.GC_KEEP)

    # Dedup boosts (it's annoying to see the same note multipe times on the same page)
    if not find_one_activity(
        {
            **in_inbox(),
            **by_type([ap.ActivityType.CRETATE, ap.ActivityType.ANNOUNCE]),
            **by_object_id(obj.id),
            **flag(MetaKey.STREAM, True),
            **published_after(datetime.now(timezone.utc) - timedelta(hours=12)),
        }
    ):
        # Display it in the stream only it not there already (only looking at the last 12 hours)
        _set_flag(new_meta, MetaKey.STREAM)

    return None


@set_inbox_flags.register
def _undo_set_inbox_flags(activity: ap.Undo, new_meta: _NewMeta) -> None:
    _logger.info(f"set_inbox_flags activity={activity!r}")
    obj = activity.get_object()

    if obj.has_type(ap.ActivityType.FOLLOW):
        # Flag it as a noticiation (for the "$actor unfollowed you"
        _flag_as_notification(activity, new_meta)

        # Also set the "keep mark" for the GC (as we want to keep it forever)
        _set_flag(new_meta, MetaKey.GC_KEEP)

    return None


@set_inbox_flags.register
def _create_set_inbox_flags(activity: ap.Create, new_meta: _NewMeta) -> None:
    _logger.info(f"set_inbox_flags activity={activity!r}")
    obj = activity.get_object()

    _set_flag(new_meta, MetaKey.POLL_ANSWER, False)

    in_reply_to = obj.get_in_reply_to()

    # Check if it's a local reply
    if in_reply_to and _is_local(in_reply_to):
        # TODO(tsileo): fetch the reply to check for poll answers more precisely
        # reply_of = ap.fetch_remote_activity(in_reply_to)

        # Ensure it's not a poll answer
        if obj.name and not obj.content:
            _set_flag(new_meta, MetaKey.POLL_ANSWER)
            return None

        # Flag it as a notification
        _flag_as_notification(activity, new_meta)

        # Also set the "keep mark" for the GC (as we want to keep it forever)
        _set_flag(new_meta, MetaKey.GC_KEEP)

        return None

    # Check for mention
    for mention in obj.get_mentions():
        if mention.href and _is_local(mention.href):
            # Flag it as a notification
            _flag_as_notification(activity, new_meta)

            # Also set the "keep mark" for the GC (as we want to keep it forever)
            _set_flag(new_meta, MetaKey.GC_KEEP)

    if not in_reply_to:
        # A good candidate for displaying in the stream
        _set_flag(new_meta, MetaKey.STREAM)

    return None
