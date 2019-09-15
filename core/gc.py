import logging
from datetime import datetime
from datetime import timedelta
from time import perf_counter
from typing import Any
from typing import Dict
from typing import List

from little_boxes import activitypub as ap
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import RemoteServerUnavailableError

from config import DAYS_TO_KEEP
from config import ID
from config import ME
from config import MEDIA_CACHE
from core import activitypub
from core.meta import Box
from core.meta import MetaKey
from core.meta import _meta
from core.meta import by_type
from core.meta import in_inbox
from utils.migrations import DB

back = activitypub.MicroblogPubBackend()
ap.use_backend(back)

MY_PERSON = ap.Person(**ME)

logger = logging.getLogger(__name__)


def threads_of_interest() -> List[str]:
    out = set()

    # Fetch all the threads we've participed in
    for data in DB.activities.find(
        {
            "meta.thread_root_parent": {"$exists": True},
            "box": Box.OUTBOX.value,
            "type": ap.ActivityType.CREATE.value,
        }
    ):
        out.add(data["meta"]["thread_root_parent"])

    # Fetch all threads related to bookmarked activities
    for data in DB.activities.find({"meta.bookmarked": True}):
        # Keep the replies
        out.add(data["meta"]["object_id"])
        # And the whole thread if any
        if "thread_root_parent" in data["meta"]:
            out.add(data["meta"]["thread_root_parent"])

    return list(out)


def _keep(data: Dict[str, Any]) -> None:
    DB.activities.update_one({"_id": data["_id"]}, {"$set": {"meta.gc_keep": True}})


def perform() -> None:  # noqa: C901
    start = perf_counter()
    d = (datetime.utcnow() - timedelta(days=DAYS_TO_KEEP)).strftime("%Y-%m-%d")
    toi = threads_of_interest()
    logger.info(f"thread_of_interest={toi!r}")

    delete_deleted = DB.activities.delete_many(
        {
            **in_inbox(),
            **by_type(ap.ActivityType.DELETE),
            _meta(MetaKey.PUBLISHED): {"$lt": d},
        }
    ).deleted_count
    logger.info(f"{delete_deleted} Delete deleted")

    create_deleted = 0
    create_count = 0
    # Go over the old Create activities
    for data in DB.activities.find(
        {
            "box": Box.INBOX.value,
            "type": ap.ActivityType.CREATE.value,
            _meta(MetaKey.PUBLISHED): {"$lt": d},
            "meta.gc_keep": {"$exists": False},
        }
    ).limit(500):
        try:
            logger.info(f"data={data!r}")
            create_count += 1
            remote_id = data["remote_id"]
            meta = data["meta"]

            # This activity has been bookmarked, keep it
            if meta.get("bookmarked"):
                _keep(data)
                continue

            obj = None
            if not meta.get("deleted"):
                try:
                    activity = ap.parse_activity(data["activity"])
                    logger.info(f"activity={activity!r}")
                    obj = activity.get_object()
                except (RemoteServerUnavailableError, ActivityGoneError):
                    logger.exception(
                        f"failed to load {remote_id}, this activity will be deleted"
                    )

            # This activity mentions the server actor, keep it
            if obj and obj.has_mention(ID):
                _keep(data)
                continue

            # This activity is a direct reply of one the server actor activity, keep it
            if obj:
                in_reply_to = obj.get_in_reply_to()
                if in_reply_to and in_reply_to.startswith(ID):
                    _keep(data)
                    continue

            # This activity is part of a thread we want to keep, keep it
            if obj and in_reply_to and meta.get("thread_root_parent"):
                thread_root_parent = meta["thread_root_parent"]
                if thread_root_parent.startswith(ID) or thread_root_parent in toi:
                    _keep(data)
                    continue

            # This activity was boosted or liked, keep it
            if meta.get("boosted") or meta.get("liked"):
                _keep(data)
                continue

            # TODO(tsileo): remove after tests
            if meta.get("keep"):
                logger.warning(
                    f"{activity!r} would not have been deleted, skipping for now"
                )
                _keep(data)
                continue

            # Delete the cached attachment
            for grid_item in MEDIA_CACHE.fs.find({"remote_id": remote_id}):
                MEDIA_CACHE.fs.delete(grid_item._id)

            # Delete the activity
            DB.activities.delete_one({"_id": data["_id"]})
            create_deleted += 1
        except Exception:
            logger.exception(f"failed to process {data!r}")

    for data in DB.replies.find(
        {_meta(MetaKey.PUBLISHED): {"$lt": d}, "meta.gc_keep": {"$exists": False}}
    ).limit(500):
        try:
            logger.info(f"data={data!r}")
            create_count += 1
            remote_id = data["remote_id"]
            meta = data["meta"]

            # This activity has been bookmarked, keep it
            if meta.get("bookmarked"):
                _keep(data)
                continue

            obj = ap.parse_activity(data["activity"])
            # This activity is a direct reply of one the server actor activity, keep it
            in_reply_to = obj.get_in_reply_to()

            # This activity is part of a thread we want to keep, keep it
            if in_reply_to and meta.get("thread_root_parent"):
                thread_root_parent = meta["thread_root_parent"]
                if thread_root_parent.startswith(ID) or thread_root_parent in toi:
                    _keep(data)
                    continue

            # This activity was boosted or liked, keep it
            if meta.get("boosted") or meta.get("liked"):
                _keep(data)
                continue

            # Delete the cached attachment
            for grid_item in MEDIA_CACHE.fs.find({"remote_id": remote_id}):
                MEDIA_CACHE.fs.delete(grid_item._id)

            # Delete the activity
            DB.replies.delete_one({"_id": data["_id"]})
            create_deleted += 1
        except Exception:
            logger.exception(f"failed to process {data!r}")

    after_gc_create = perf_counter()
    time_to_gc_create = after_gc_create - start
    logger.info(
        f"{time_to_gc_create:.2f} seconds to analyze {create_count} Create, {create_deleted} deleted"
    )

    announce_count = 0
    announce_deleted = 0
    # Go over the old Create activities
    for data in DB.activities.find(
        {
            "box": Box.INBOX.value,
            "type": ap.ActivityType.ANNOUNCE.value,
            _meta(MetaKey.PUBLISHED): {"$lt": d},
            "meta.gc_keep": {"$exists": False},
        }
    ).limit(500):
        try:
            announce_count += 1
            remote_id = data["remote_id"]
            meta = data["meta"]
            activity = ap.parse_activity(data["activity"])
            logger.info(f"activity={activity!r}")

            # This activity has been bookmarked, keep it
            if meta.get("bookmarked"):
                _keep(data)
                continue

            object_id = activity.get_object_id()

            # This announce is for a local activity (i.e. from the outbox), keep it
            if object_id.startswith(ID):
                _keep(data)
                continue

            for grid_item in MEDIA_CACHE.fs.find({"remote_id": remote_id}):
                MEDIA_CACHE.fs.delete(grid_item._id)

            # TODO(tsileo): here for legacy reason, this needs to be removed at some point
            for grid_item in MEDIA_CACHE.fs.find({"remote_id": object_id}):
                MEDIA_CACHE.fs.delete(grid_item._id)

            # Delete the activity
            DB.activities.delete_one({"_id": data["_id"]})

            announce_deleted += 1
        except Exception:
            logger.exception(f"failed to process {data!r}")

    after_gc_announce = perf_counter()
    time_to_gc_announce = after_gc_announce - after_gc_create
    logger.info(
        f"{time_to_gc_announce:.2f} seconds to analyze {announce_count} Announce, {announce_deleted} deleted"
    )
