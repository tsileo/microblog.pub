"""Migrations that will be run automatically at startup."""
from typing import Any
from typing import Dict
from urllib.parse import urlparse

from little_boxes import activitypub as ap

import activitypub
from config import ID
from utils.migrations import DB
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
                logger.exception("failed to process actor {data!r}")
