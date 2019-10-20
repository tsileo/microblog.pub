"""Migrations that will be run automatically at startup."""
from typing import Any
from typing import Dict
from urllib.parse import urlparse

from little_boxes import activitypub as ap

from config import ID
from core import activitypub
from core.db import DB
from core.db import find_activities
from core.db import update_one_activity
from core.meta import FollowStatus
from core.meta import MetaKey
from core.meta import _meta
from core.meta import by_actor_id
from core.meta import by_object_id
from core.meta import by_remote_id
from core.meta import by_type
from core.meta import in_inbox
from core.meta import in_outbox
from core.meta import not_deleted
from core.meta import not_undo
from core.meta import upsert
from utils.migrations import Migration
from utils.migrations import logger
from utils.migrations import perform  # noqa:  just here for export

back = activitypub.MicroblogPubBackend()
ap.use_backend(back)


class _1_MetaMigration(Migration):
    """Add new metadata to simplify querying."""

    def __guess_visibility(self, data: Dict[str, Any]) -> ap.Visibility:
        to = data.get("to", [])
        cc = data.get("cc", [])
        if ap.AS_PUBLIC in to:
            return ap.Visibility.PUBLIC
        elif ap.AS_PUBLIC in cc:
            return ap.Visibility.UNLISTED
        else:
            # Uses a bit of heuristic here, it's too expensive to fetch the actor, so assume the followers
            # collection has "/collection" in it (which is true for most software), and at worst, we will
            # classify it as "DIRECT" which behave the same as "FOLLOWERS_ONLY" (i.e. no Announce)
            followers_only = False
            for item in to:
                if "/followers" in item:
                    followers_only = True
                    break
            if not followers_only:
                for item in cc:
                    if "/followers" in item:
                        followers_only = True
                        break
            if followers_only:
                return ap.Visibility.FOLLOWERS_ONLY

        return ap.Visibility.DIRECT

    def migrate(self) -> None:  # noqa: C901  # too complex
        for data in DB.activities.find():
            logger.info(f"before={data}")
            obj = data["activity"].get("object")
            set_meta: Dict[str, Any] = {}

            # Set `meta.object_id` (str)
            if not data["meta"].get("object_id"):
                set_meta["meta.object_id"] = None
                if obj:
                    if isinstance(obj, str):
                        set_meta["meta.object_id"] = data["activity"]["object"]
                    elif isinstance(obj, dict):
                        obj_id = obj.get("id")
                        if obj_id:
                            set_meta["meta.object_id"] = obj_id

            # Set `meta.object_visibility` (str)
            if not data["meta"].get("object_visibility"):
                set_meta["meta.object_visibility"] = None
                object_id = data["meta"].get("object_id") or set_meta.get(
                    "meta.object_id"
                )
                if object_id:
                    obj = data["meta"].get("object") or data["activity"].get("object")
                    if isinstance(obj, dict):
                        set_meta["meta.object_visibility"] = self.__guess_visibility(
                            obj
                        ).name

            # Set `meta.actor_id` (str)
            if not data["meta"].get("actor_id"):
                set_meta["meta.actor_id"] = None
                actor = data["activity"].get("actor")
                if actor:
                    if isinstance(actor, str):
                        set_meta["meta.actor_id"] = data["activity"]["actor"]
                    elif isinstance(actor, dict):
                        actor_id = actor.get("id")
                        if actor_id:
                            set_meta["meta.actor_id"] = actor_id

            # Set `meta.poll_answer` (bool)
            if not data["meta"].get("poll_answer"):
                set_meta["meta.poll_answer"] = False
                if obj:
                    if isinstance(obj, dict):
                        if (
                            obj.get("name")
                            and not obj.get("content")
                            and obj.get("inReplyTo")
                        ):
                            set_meta["meta.poll_answer"] = True

            # Set `meta.visibility` (str)
            if not data["meta"].get("visibility"):
                set_meta["meta.visibility"] = self.__guess_visibility(
                    data["activity"]
                ).name

            if not data["meta"].get("server"):
                set_meta["meta.server"] = urlparse(data["remote_id"]).netloc

            logger.info(f"meta={set_meta}\n")
            if set_meta:
                DB.activities.update_one({"_id": data["_id"]}, {"$set": set_meta})


class _2_FollowMigration(Migration):
    """Add new metadata to update the cached actor in Follow activities."""

    def migrate(self) -> None:
        actor_cache: Dict[str, Dict[str, Any]] = {}
        for data in DB.activities.find({"type": ap.ActivityType.FOLLOW.value}):
            try:
                if data["meta"]["actor_id"] == ID:
                    # It's a "following"
                    actor = actor_cache.get(data["meta"]["object_id"])
                    if not actor:
                        actor = ap.parse_activity(
                            ap.get_backend().fetch_iri(
                                data["meta"]["object_id"], no_cache=True
                            )
                        ).to_dict(embed=True)
                        if not actor:
                            raise ValueError(f"missing actor {data!r}")
                        actor_cache[actor["id"]] = actor
                    DB.activities.update_one(
                        {"_id": data["_id"]}, {"$set": {"meta.object": actor}}
                    )

                else:
                    # It's a "followers"
                    actor = actor_cache.get(data["meta"]["actor_id"])
                    if not actor:
                        actor = ap.parse_activity(
                            ap.get_backend().fetch_iri(
                                data["meta"]["actor_id"], no_cache=True
                            )
                        ).to_dict(embed=True)
                        if not actor:
                            raise ValueError(f"missing actor {data!r}")
                        actor_cache[actor["id"]] = actor
                    DB.activities.update_one(
                        {"_id": data["_id"]}, {"$set": {"meta.actor": actor}}
                    )
            except Exception:
                logger.exception(f"failed to process actor {data!r}")


class _20190830_MetaPublishedMigration(Migration):
    """Add the `meta.published` field to old activities."""

    def migrate(self) -> None:
        for data in find_activities({"meta.published": {"$exists": False}}):
            try:
                raw = data["activity"]
                # If the activity has its own `published` field, we'll use it
                if "published" in raw:
                    published = raw["published"]
                else:
                    # Otherwise, we take the date we received the activity as the published time
                    published = ap.format_datetime(data["_id"].generation_time)

                # Set the field in the DB
                update_one_activity(
                    {"_id": data["_id"]},
                    {"$set": {_meta(MetaKey.PUBLISHED): published}},
                )

            except Exception:
                logger.exception(f"failed to process activity {data!r}")


class _20190830_FollowFollowBackMigration(Migration):
    """Add the new meta flags for tracking accepted/rejected status and following/follows back info."""

    def migrate(self) -> None:
        for data in find_activities({**by_type(ap.ActivityType.ACCEPT), **in_inbox()}):
            try:
                update_one_activity(
                    {
                        **by_type(ap.ActivityType.FOLLOW),
                        **by_remote_id(data["meta"]["object_id"]),
                    },
                    upsert({MetaKey.FOLLOW_STATUS: FollowStatus.ACCEPTED.value}),
                )
                # Check if we are following this actor
                follow_query = {
                    **in_inbox(),
                    **by_type(ap.ActivityType.FOLLOW),
                    **by_actor_id(data["meta"]["actor_id"]),
                    **not_undo(),
                }
                raw_follow = DB.activities.find_one(follow_query)
                if raw_follow:
                    DB.activities.update_many(
                        follow_query,
                        {"$set": {_meta(MetaKey.NOTIFICATION_FOLLOWS_BACK): True}},
                    )

            except Exception:
                logger.exception(f"failed to process activity {data!r}")

        for data in find_activities({**by_type(ap.ActivityType.REJECT), **in_inbox()}):
            try:
                update_one_activity(
                    {
                        **by_type(ap.ActivityType.FOLLOW),
                        **by_remote_id(data["meta"]["object_id"]),
                    },
                    upsert({MetaKey.FOLLOW_STATUS: FollowStatus.REJECTED.value}),
                )
            except Exception:
                logger.exception(f"failed to process activity {data!r}")

        DB.activities.update_many(
            {
                **by_type(ap.ActivityType.FOLLOW),
                **in_inbox(),
                "meta.follow_status": {"$exists": False},
            },
            {"$set": {"meta.follow_status": "waiting"}},
        )


class _20190901_FollowFollowBackMigrationFix(Migration):
    """Add the new meta flags for tracking accepted/rejected status and following/follows back info."""

    def migrate(self) -> None:
        for data in find_activities({**by_type(ap.ActivityType.ACCEPT), **in_inbox()}):
            try:
                update_one_activity(
                    {
                        **by_type(ap.ActivityType.FOLLOW),
                        **by_remote_id(data["meta"]["object_id"]),
                    },
                    upsert({MetaKey.FOLLOW_STATUS: FollowStatus.ACCEPTED.value}),
                )
                # Check if we are following this actor
                follow_query = {
                    **in_inbox(),
                    **by_type(ap.ActivityType.FOLLOW),
                    **by_object_id(data["meta"]["actor_id"]),
                    **not_undo(),
                }
                raw_follow = DB.activities.find_one(follow_query)
                if raw_follow:
                    DB.activities.update_many(
                        follow_query,
                        {"$set": {_meta(MetaKey.NOTIFICATION_FOLLOWS_BACK): True}},
                    )

            except Exception:
                logger.exception(f"failed to process activity {data!r}")

        for data in find_activities({**by_type(ap.ActivityType.FOLLOW), **in_outbox()}):
            try:
                print(data)
                follow_query = {
                    **in_inbox(),
                    **by_type(ap.ActivityType.FOLLOW),
                    **by_actor_id(data["meta"]["object_id"]),
                    **not_undo(),
                }
                raw_accept = DB.activities.find_one(follow_query)
                print(raw_accept)
                if raw_accept:
                    DB.activities.update_many(
                        by_remote_id(data["remote_id"]),
                        {"$set": {_meta(MetaKey.NOTIFICATION_FOLLOWS_BACK): True}},
                    )

            except Exception:
                logger.exception(f"failed to process activity {data!r}")


class _20190901_MetaHashtagsAndMentions(Migration):
    def migrate(self) -> None:
        for data in find_activities(
            {**by_type(ap.ActivityType.CREATE), **not_deleted()}
        ):
            try:
                activity = ap.parse_activity(data["activity"])
                mentions = []
                obj = activity.get_object()
                for m in obj.get_mentions():
                    mentions.append(m.href)
                hashtags = []
                for h in obj.get_hashtags():
                    hashtags.append(h.name[1:])  # Strip the #

                update_one_activity(
                    by_remote_id(data["remote_id"]),
                    upsert({MetaKey.MENTIONS: mentions, MetaKey.HASHTAGS: hashtags}),
                )

            except Exception:
                logger.exception(f"failed to process activity {data!r}")


class _20190906_RedoFollowFollowBack(_20190901_FollowFollowBackMigrationFix):
    """Add the new meta flags for tracking accepted/rejected status and following/follows back info."""


class _20190906_InReplyToMigration(Migration):
    def migrate(self) -> None:
        for data in find_activities(
            {**by_type(ap.ActivityType.CREATE), **not_deleted()}
        ):
            try:
                in_reply_to = data["activity"]["object"].get("inReplyTo")
                if in_reply_to:
                    update_one_activity(
                        by_remote_id(data["remote_id"]),
                        upsert({MetaKey.IN_REPLY_TO: in_reply_to}),
                    )
            except Exception:
                logger.exception(f"failed to process activity {data!r}")

        for data in DB.replies.find({**not_deleted()}):
            try:
                in_reply_to = data["activity"].get("inReplyTo")
                if in_reply_to:
                    DB.replies.update_one(
                        by_remote_id(data["remote_id"]),
                        upsert({MetaKey.IN_REPLY_TO: in_reply_to}),
                    )
            except Exception:
                logger.exception(f"failed to process activity {data!r}")


class _20191020_ManuallyApprovesFollowerSupportMigrationn(Migration):
    def migrate(self) -> None:
        DB.activities.update_many(
            {
                **by_type(ap.ActivityType.FOLLOW),
                **in_inbox(),
                "meta.follow_status": {"$exists": False},
            },
            {"$set": {"meta.follow_status": "accepted"}},
        )
