import logging
from datetime import datetime
from datetime import timedelta
from typing import List

from little_boxes import activitypub as ap

import activitypub
from activitypub import Box
from config import ID
from config import ME
from config import MEDIA_CACHE
from config import DAYS_TO_KEEP
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


def perform() -> None:
    d = (datetime.utcnow() - timedelta(days=DAYS_TO_KEEP)).strftime("%Y-%m-%d")
    toi = threads_of_interest()
    logger.info(f"thread_of_interest={toi!r}")

    # Go over the old Create activities
    for data in DB.activities.find(
        {
            "box": Box.INBOX.value,
            "type": ap.ActivityType.CREATE.value,
            "activity.published": {"$lt": d},
        }
    ):
        try:
            remote_id = data["remote_id"]
            meta = data["meta"]
            activity = ap.parse_activity(data["activity"])
            logger.info(f"activity={activity!r}")

            # This activity has been bookmarked, keep it
            if meta.get("bookmarked"):
                continue

            # Inspect the object
            obj = activity.get_object()

            # This activity mentions the server actor, keep it
            if obj.has_mention(ID):
                continue

            # This activity is a direct reply of one the server actor activity, keep it
            in_reply_to = obj.get_in_reply_to()
            if in_reply_to and in_reply_to.startswith(ID):
                continue

            # This activity is part of a thread we want to keep, keep it
            if in_reply_to and meta.get("thread_root_parent"):
                thread_root_parent = meta["thread_root_parent"]
                if thread_root_parent.startswith(ID) or thread_root_parent in toi:
                    continue

            # This activity was boosted or liked, keep it
            if meta.get("boosted") or meta.get("liked"):
                continue

            # TODO(tsileo): remove after tests
            if meta.get("keep"):
                logger.warning(f"{activity!r} would not have been deleted, skipping for now")
                continue

            # Delete the cached attachment
            for grid_item in MEDIA_CACHE.fs.find({"remote_id": remote_id}):
                MEDIA_CACHE.fs.delete(grid_item._id)

            # Delete the activity
            DB.activities.delete_one({"_id": data["_id"]})
        except Exception:
            logger.exception(f"failed to process {data!r}")
