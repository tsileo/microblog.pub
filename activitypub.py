import logging
import os
import json
from datetime import datetime
from enum import Enum
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from bson.objectid import ObjectId
from cachetools import LRUCache
from feedgen.feed import FeedGenerator
from html2text import html2text
from little_boxes import activitypub as ap
from little_boxes import strtobool
from little_boxes.activitypub import _to_list
from little_boxes.backend import Backend
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import Error
from little_boxes.errors import NotAnActivityError

from config import BASE_URL
from config import DB
from config import EXTRA_INBOXES
from config import ID
from config import ME
from config import USER_AGENT
from config import USERNAME

logger = logging.getLogger(__name__)


ACTORS_CACHE = LRUCache(maxsize=256)


def _actor_to_meta(actor: ap.BaseActivity, with_inbox=False) -> Dict[str, Any]:
    meta = {
        "id": actor.id,
        "url": actor.url,
        "icon": actor.icon,
        "name": actor.name,
        "preferredUsername": actor.preferredUsername,
    }
    if with_inbox:
        meta.update(
            {
                "inbox": actor.inbox,
                "sharedInbox": actor._data.get("endpoints", {}).get("sharedInbox"),
            }
        )
    logger.debug(f"meta={meta}")

    return meta


def _remove_id(doc: ap.ObjectType) -> ap.ObjectType:
    """Helper for removing MongoDB's `_id` field."""
    doc = doc.copy()
    if "_id" in doc:
        del (doc["_id"])
    return doc


def ensure_it_is_me(f):
    """Method decorator used to track the events fired during tests."""

    def wrapper(*args, **kwargs):
        if args[1].id != ME["id"]:
            raise Error("unexpected actor")
        return f(*args, **kwargs)

    return wrapper


class Box(Enum):
    INBOX = "inbox"
    OUTBOX = "outbox"
    REPLIES = "replies"


class MicroblogPubBackend(Backend):
    """Implements a Little Boxes backend, backed by MongoDB."""

    def debug_mode(self) -> bool:
        return strtobool(os.getenv("MICROBLOGPUB_DEBUG", "false"))

    def user_agent(self) -> str:
        """Setup a custom user agent."""
        return USER_AGENT

    def extra_inboxes(self) -> List[str]:
        return EXTRA_INBOXES

    def base_url(self) -> str:
        """Base URL config."""
        return BASE_URL

    def activity_url(self, obj_id):
        """URL for activity link."""
        return f"{BASE_URL}/outbox/{obj_id}"

    def note_url(self, obj_id):
        """URL for activity link."""
        return f"{BASE_URL}/note/{obj_id}"

    def save(self, box: Box, activity: ap.BaseActivity) -> None:
        """Custom helper for saving an activity to the DB."""
        DB.activities.insert_one(
            {
                "box": box.value,
                "activity": activity.to_dict(),
                "type": _to_list(activity.type),
                "remote_id": activity.id,
                "meta": {"undo": False, "deleted": False},
            }
        )

    def followers(self) -> List[str]:
        q = {
            "box": Box.INBOX.value,
            "type": ap.ActivityType.FOLLOW.value,
            "meta.undo": False,
        }
        return [doc["activity"]["actor"] for doc in DB.activities.find(q)]

    def followers_as_recipients(self) -> List[str]:
        q = {
            "box": Box.INBOX.value,
            "type": ap.ActivityType.FOLLOW.value,
            "meta.undo": False,
        }
        recipients = []
        for doc in DB.activities.find(q):
            recipients.append(
                doc["meta"]["actor"]["sharedInbox"] or doc["meta"]["actor"]["inbox"]
            )

        return list(set(recipients))

    def following(self) -> List[str]:
        q = {
            "box": Box.OUTBOX.value,
            "type": ap.ActivityType.FOLLOW.value,
            "meta.undo": False,
        }
        return [doc["activity"]["object"] for doc in DB.activities.find(q)]

    def parse_collection(
        self, payload: Optional[Dict[str, Any]] = None, url: Optional[str] = None
    ) -> List[str]:
        """Resolve/fetch a `Collection`/`OrderedCollection`."""
        # Resolve internal collections via MongoDB directly
        if url == ID + "/followers":
            return self.followers()
        elif url == ID + "/following":
            return self.following()

        return super().parse_collection(payload, url)

    @ensure_it_is_me
    def outbox_is_blocked(self, as_actor: ap.Person, actor_id: str) -> bool:
        return bool(
            DB.activities.find_one(
                {
                    "box": Box.OUTBOX.value,
                    "type": ap.ActivityType.BLOCK.value,
                    "activity.object": actor_id,
                    "meta.undo": False,
                }
            )
        )

    def _fetch_iri(self, iri: str) -> ap.ObjectType:
        if iri == ME["id"]:
            return ME

        # Check if the activity is owned by this server
        if iri.startswith(BASE_URL):
            is_a_note = False
            if iri.endswith("/activity"):
                iri = iri.replace("/activity", "")
                is_a_note = True
            data = DB.activities.find_one({"box": Box.OUTBOX.value, "remote_id": iri})
            if data and data["meta"]["deleted"]:
                raise ActivityGoneError(f"{iri} is gone")
            if data and is_a_note:
                return data["activity"]["object"]
            elif data:
                return data["activity"]
        else:
            # Check if the activity is stored in the inbox
            data = DB.activities.find_one({"remote_id": iri})
            if data:
                if data["meta"]["deleted"]:
                    raise ActivityGoneError(f"{iri} is gone")
                return data["activity"]

        # Fetch the URL via HTTP
        logger.info(f"dereference {iri} via HTTP")
        return super().fetch_iri(iri)

    def fetch_iri(self, iri: str) -> ap.ObjectType:
        if iri == ME["id"]:
            return ME

        if iri in ACTORS_CACHE:
            logger.info(f"{iri} found in cache")
            return ACTORS_CACHE[iri]

        # data = DB.actors.find_one({"remote_id": iri})
        # if data:
        #    if ap._has_type(data["type"], ap.ACTOR_TYPES):
        #        logger.info(f"{iri} found in DB cache")
        #        ACTORS_CACHE[iri] = data["data"]
        #    return data["data"]

        data = self._fetch_iri(iri)
        logger.debug(f"_fetch_iri({iri!r}) == {data!r}")
        if ap._has_type(data["type"], ap.ACTOR_TYPES):
            logger.debug(f"caching actor {iri}")
            # Cache the actor
            DB.actors.update_one(
                {"remote_id": iri},
                {"$set": {"remote_id": iri, "data": data}},
                upsert=True,
            )
            ACTORS_CACHE[iri] = data

        return data

    @ensure_it_is_me
    def inbox_check_duplicate(self, as_actor: ap.Person, iri: str) -> bool:
        return bool(DB.activities.find_one({"box": Box.INBOX.value, "remote_id": iri}))

    def set_post_to_remote_inbox(self, cb):
        self.post_to_remote_inbox_cb = cb

    @ensure_it_is_me
    def undo_new_follower(self, as_actor: ap.Person, follow: ap.Follow) -> None:
        DB.activities.update_one(
            {"remote_id": follow.id}, {"$set": {"meta.undo": True}}
        )

    @ensure_it_is_me
    def undo_new_following(self, as_actor: ap.Person, follow: ap.Follow) -> None:
        DB.activities.update_one(
            {"remote_id": follow.id}, {"$set": {"meta.undo": True}}
        )

    @ensure_it_is_me
    def inbox_like(self, as_actor: ap.Person, like: ap.Like) -> None:
        obj = like.get_object()
        # Update the meta counter if the object is published by the server
        DB.activities.update_one(
            {"box": Box.OUTBOX.value, "activity.object.id": obj.id},
            {"$inc": {"meta.count_like": 1}},
        )

    @ensure_it_is_me
    def inbox_undo_like(self, as_actor: ap.Person, like: ap.Like) -> None:
        obj = like.get_object()
        # Update the meta counter if the object is published by the server
        DB.activities.update_one(
            {"box": Box.OUTBOX.value, "activity.object.id": obj.id},
            {"$inc": {"meta.count_like": -1}},
        )
        DB.activities.update_one({"remote_id": like.id}, {"$set": {"meta.undo": True}})

    @ensure_it_is_me
    def outbox_like(self, as_actor: ap.Person, like: ap.Like) -> None:
        obj = like.get_object()
        DB.activities.update_one(
            {"activity.object.id": obj.id},
            {"$inc": {"meta.count_like": 1}, "$set": {"meta.liked": like.id}},
        )

    @ensure_it_is_me
    def outbox_undo_like(self, as_actor: ap.Person, like: ap.Like) -> None:
        obj = like.get_object()
        DB.activities.update_one(
            {"activity.object.id": obj.id},
            {"$inc": {"meta.count_like": -1}, "$set": {"meta.liked": False}},
        )
        DB.activities.update_one({"remote_id": like.id}, {"$set": {"meta.undo": True}})

    @ensure_it_is_me
    def inbox_announce(self, as_actor: ap.Person, announce: ap.Announce) -> None:
        # TODO(tsileo): actually drop it without storing it and better logging, also move the check somewhere else
        # or remove it?
        try:
            obj = announce.get_object()
        except NotAnActivityError:
            logger.exception(
                f'received an Annouce referencing an OStatus notice ({announce._data["object"]}), dropping the message'
            )
            return

        DB.activities.update_one(
            {"remote_id": announce.id},
            {
                "$set": {
                    "meta.object": obj.to_dict(embed=True),
                    "meta.object_actor": _actor_to_meta(obj.get_actor()),
                }
            },
        )
        DB.activities.update_one(
            {"activity.object.id": obj.id}, {"$inc": {"meta.count_boost": 1}}
        )

    @ensure_it_is_me
    def inbox_undo_announce(self, as_actor: ap.Person, announce: ap.Announce) -> None:
        obj = announce.get_object()
        # Update the meta counter if the object is published by the server
        DB.activities.update_one(
            {"activity.object.id": obj.id}, {"$inc": {"meta.count_boost": -1}}
        )
        DB.activities.update_one(
            {"remote_id": announce.id}, {"$set": {"meta.undo": True}}
        )

    @ensure_it_is_me
    def outbox_announce(self, as_actor: ap.Person, announce: ap.Announce) -> None:
        obj = announce.get_object()
        DB.activities.update_one(
            {"remote_id": announce.id},
            {
                "$set": {
                    "meta.object": obj.to_dict(embed=True),
                    "meta.object_actor": _actor_to_meta(obj.get_actor()),
                }
            },
        )

        DB.activities.update_one(
            {"activity.object.id": obj.id}, {"$set": {"meta.boosted": announce.id}}
        )

    @ensure_it_is_me
    def outbox_undo_announce(self, as_actor: ap.Person, announce: ap.Announce) -> None:
        obj = announce.get_object()
        DB.activities.update_one(
            {"activity.object.id": obj.id}, {"$set": {"meta.boosted": False}}
        )
        DB.activities.update_one(
            {"remote_id": announce.id}, {"$set": {"meta.undo": True}}
        )

    @ensure_it_is_me
    def inbox_delete(self, as_actor: ap.Person, delete: ap.Delete) -> None:
        obj = delete.get_object()
        logger.debug("delete object={obj!r}")
        DB.activities.update_one(
            {"activity.object.id": obj.id}, {"$set": {"meta.deleted": True}}
        )

        logger.info(f"inbox_delete handle_replies obj={obj!r}")
        in_reply_to = obj.inReplyTo
        if delete.get_object().ACTIVITY_TYPE != ap.ActivityType.NOTE:
            in_reply_to = DB.activities.find_one(
                {
                    "activity.object.id": delete.get_object().id,
                    "type": ap.ActivityType.CREATE.value,
                }
            )["activity"]["object"].get("inReplyTo")

        # Fake a Undo so any related Like/Announce doesn't appear on the web UI
        DB.activities.update(
            {"meta.object.id": obj.id},
            {"$set": {"meta.undo": True, "meta.extra": "object deleted"}},
        )
        if in_reply_to:
            self._handle_replies_delete(as_actor, in_reply_to)

    @ensure_it_is_me
    def outbox_delete(self, as_actor: ap.Person, delete: ap.Delete) -> None:
        DB.activities.update_one(
            {"activity.object.id": delete.get_object().id},
            {"$set": {"meta.deleted": True}},
        )
        obj = delete.get_object()
        if delete.get_object().ACTIVITY_TYPE != ap.ActivityType.NOTE:
            obj = ap.parse_activity(
                DB.activities.find_one(
                    {
                        "activity.object.id": delete.get_object().id,
                        "type": ap.ActivityType.CREATE.value,
                    }
                )["activity"]
            ).get_object()

        DB.activities.update(
            {"meta.object.id": obj.id},
            {"$set": {"meta.undo": True, "meta.exta": "object deleted"}},
        )

        self._handle_replies_delete(as_actor, obj.inReplyTo)

    @ensure_it_is_me
    def inbox_update(self, as_actor: ap.Person, update: ap.Update) -> None:
        obj = update.get_object()
        if obj.ACTIVITY_TYPE == ap.ActivityType.NOTE:
            DB.activities.update_one(
                {"activity.object.id": obj.id},
                {"$set": {"activity.object": obj.to_dict()}},
            )
        # FIXME(tsileo): handle update actor amd inbox_update_note/inbox_update_actor

    @ensure_it_is_me
    def outbox_update(self, as_actor: ap.Person, _update: ap.Update) -> None:
        obj = _update._data["object"]

        update_prefix = "activity.object."
        update: Dict[str, Any] = {"$set": dict(), "$unset": dict()}
        update["$set"][f"{update_prefix}updated"] = (
            datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        )
        for k, v in obj.items():
            if k in ["id", "type"]:
                continue
            if v is None:
                update["$unset"][f"{update_prefix}{k}"] = ""
            else:
                update["$set"][f"{update_prefix}{k}"] = v

        if len(update["$unset"]) == 0:
            del (update["$unset"])

        print(f"updating note from outbox {obj!r} {update}")
        logger.info(f"updating note from outbox {obj!r} {update}")
        DB.activities.update_one({"activity.object.id": obj["id"]}, update)
        # FIXME(tsileo): should send an Update (but not a partial one, to all the note's recipients
        # (create a new Update with the result of the update, and send it without saving it?)

    @ensure_it_is_me
    def outbox_create(self, as_actor: ap.Person, create: ap.Create) -> None:
        self._handle_replies(as_actor, create)

    @ensure_it_is_me
    def inbox_create(self, as_actor: ap.Person, create: ap.Create) -> None:
        self._handle_replies(as_actor, create)

    @ensure_it_is_me
    def _handle_replies_delete(
        self, as_actor: ap.Person, in_reply_to: Optional[str]
    ) -> None:
        if not in_reply_to:
            pass

        DB.activities.update_one(
            {"activity.object.id": in_reply_to},
            {"$inc": {"meta.count_reply": -1, "meta.count_direct_reply": -1}},
        )

    @ensure_it_is_me
    def _handle_replies(self, as_actor: ap.Person, create: ap.Create) -> None:
        """Go up to the root reply, store unknown replies in the `threads` DB and set the "meta.thread_root_parent"
        key to make it easy to query a whole thread."""
        in_reply_to = create.get_object().inReplyTo
        if not in_reply_to:
            return

        new_threads = []
        root_reply = in_reply_to
        reply = ap.fetch_remote_activity(root_reply)

        creply = DB.activities.find_one_and_update(
            {"activity.object.id": in_reply_to},
            {"$inc": {"meta.count_reply": 1, "meta.count_direct_reply": 1}},
        )
        if not creply:
            # It means the activity is not in the inbox, and not in the outbox, we want to save it
            self.save(Box.REPLIES, reply)
            new_threads.append(reply.id)

        while reply is not None:
            in_reply_to = reply.inReplyTo
            if not in_reply_to:
                break
            root_reply = in_reply_to
            reply = ap.fetch_remote_activity(root_reply)
            q = {"activity.object.id": root_reply}
            if not DB.activities.count(q):
                self.save(Box.REPLIES, reply)
                new_threads.append(reply.id)

        DB.activities.update_one(
            {"remote_id": create.id}, {"$set": {"meta.thread_root_parent": root_reply}}
        )
        DB.activities.update(
            {"box": Box.REPLIES.value, "remote_id": {"$in": new_threads}},
            {"$set": {"meta.thread_root_parent": root_reply}},
        )

    def post_to_outbox(self, activity: ap.BaseActivity) -> None:
        if activity.has_type(ap.CREATE_TYPES):
            activity = activity.build_create()

        self.save(Box.OUTBOX, activity)

        # Assign create a random ID
        obj_id = self.random_object_id()
        activity.set_id(self.activity_url(obj_id), obj_id)

        recipients = activity.recipients()
        logger.info(f"recipients={recipients}")
        activity = ap.clean_activity(activity.to_dict())

        payload = json.dumps(activity)
        for recp in recipients:
            logger.debug(f"posting to {recp}")
            self.post_to_remote_inbox(self.get_actor(), payload, recp)


def gen_feed():
    fg = FeedGenerator()
    fg.id(f"{ID}")
    fg.title(f"{USERNAME} notes")
    fg.author({"name": USERNAME, "email": "t@a4.io"})
    fg.link(href=ID, rel="alternate")
    fg.description(f"{USERNAME} notes")
    fg.logo(ME.get("icon", {}).get("url"))
    fg.language("en")
    for item in DB.activities.find(
        {"box": Box.OUTBOX.value, "type": "Create", "meta.deleted": False}, limit=10
    ).sort("_id", -1):
        fe = fg.add_entry()
        fe.id(item["activity"]["object"].get("url"))
        fe.link(href=item["activity"]["object"].get("url"))
        fe.title(item["activity"]["object"]["content"])
        fe.description(item["activity"]["object"]["content"])
    return fg


def json_feed(path: str) -> Dict[str, Any]:
    """JSON Feed (https://jsonfeed.org/) document."""
    data = []
    for item in DB.activities.find(
        {"box": Box.OUTBOX.value, "type": "Create", "meta.deleted": False}, limit=10
    ).sort("_id", -1):
        data.append(
            {
                "id": item["activity"]["id"],
                "url": item["activity"]["object"].get("url"),
                "content_html": item["activity"]["object"]["content"],
                "content_text": html2text(item["activity"]["object"]["content"]),
                "date_published": item["activity"]["object"].get("published"),
            }
        )
    return {
        "version": "https://jsonfeed.org/version/1",
        "user_comment": (
            "This is a microblog feed. You can add this to your feed reader using the following URL: "
            + ID
            + path
        ),
        "title": USERNAME,
        "home_page_url": ID,
        "feed_url": ID + path,
        "author": {
            "name": USERNAME,
            "url": ID,
            "avatar": ME.get("icon", {}).get("url"),
        },
        "items": data,
    }


def build_inbox_json_feed(
    path: str, request_cursor: Optional[str] = None
) -> Dict[str, Any]:
    """Build a JSON feed from the inbox activities."""
    data = []
    cursor = None

    q: Dict[str, Any] = {
        "type": "Create",
        "meta.deleted": False,
        "box": Box.INBOX.value,
    }
    if request_cursor:
        q["_id"] = {"$lt": request_cursor}

    for item in DB.activities.find(q, limit=50).sort("_id", -1):
        actor = ap.get_backend().fetch_iri(item["activity"]["actor"])
        data.append(
            {
                "id": item["activity"]["id"],
                "url": item["activity"]["object"].get("url"),
                "content_html": item["activity"]["object"]["content"],
                "content_text": html2text(item["activity"]["object"]["content"]),
                "date_published": item["activity"]["object"].get("published"),
                "author": {
                    "name": actor.get("name", actor.get("preferredUsername")),
                    "url": actor.get("url"),
                    "avatar": actor.get("icon", {}).get("url"),
                },
            }
        )
        cursor = str(item["_id"])

    resp = {
        "version": "https://jsonfeed.org/version/1",
        "title": f"{USERNAME}'s stream",
        "home_page_url": ID,
        "feed_url": ID + path,
        "items": data,
    }
    if cursor and len(data) == 50:
        resp["next_url"] = ID + path + "?cursor=" + cursor

    return resp


def embed_collection(total_items, first_page_id):
    """Helper creating a root OrderedCollection with a link to the first page."""
    return {
        "type": ap.ActivityType.ORDERED_COLLECTION.value,
        "totalItems": total_items,
        "first": f"{first_page_id}?page=first",
        "id": first_page_id,
    }


def simple_build_ordered_collection(col_name, data):
    return {
        "@context": ap.COLLECTION_CTX,
        "id": BASE_URL + "/" + col_name,
        "totalItems": len(data),
        "type": ap.ActivityType.ORDERED_COLLECTION.value,
        "orderedItems": data,
    }


def build_ordered_collection(
    col, q=None, cursor=None, map_func=None, limit=50, col_name=None, first_page=False
):
    """Helper for building an OrderedCollection from a MongoDB query (with pagination support)."""
    col_name = col_name or col.name
    if q is None:
        q = {}

    if cursor:
        q["_id"] = {"$lt": ObjectId(cursor)}
    data = list(col.find(q, limit=limit).sort("_id", -1))

    if not data:
        # Returns an empty page if there's a cursor
        if cursor:
            return {
                "@context": ap.COLLECTION_CTX,
                "type": ap.ActivityType.ORDERED_COLLECTION_PAGE.value,
                "id": BASE_URL + "/" + col_name + "?cursor=" + cursor,
                "partOf": BASE_URL + "/" + col_name,
                "totalItems": 0,
                "orderedItems": [],
            }
        return {
            "@context": ap.COLLECTION_CTX,
            "id": BASE_URL + "/" + col_name,
            "totalItems": 0,
            "type": ap.ActivityType.ORDERED_COLLECTION.value,
            "orderedItems": [],
        }

    start_cursor = str(data[0]["_id"])
    next_page_cursor = str(data[-1]["_id"])
    total_items = col.find(q).count()

    data = [_remove_id(doc) for doc in data]
    if map_func:
        data = [map_func(doc) for doc in data]

    # No cursor, this is the first page and we return an OrderedCollection
    if not cursor:
        resp = {
            "@context": ap.COLLECTION_CTX,
            "id": f"{BASE_URL}/{col_name}",
            "totalItems": total_items,
            "type": ap.ActivityType.ORDERED_COLLECTION.value,
            "first": {
                "id": f"{BASE_URL}/{col_name}?cursor={start_cursor}",
                "orderedItems": data,
                "partOf": f"{BASE_URL}/{col_name}",
                "totalItems": total_items,
                "type": ap.ActivityType.ORDERED_COLLECTION_PAGE.value,
            },
        }

        if len(data) == limit:
            resp["first"]["next"] = (
                BASE_URL + "/" + col_name + "?cursor=" + next_page_cursor
            )

        if first_page:
            return resp["first"]

        return resp

    # If there's a cursor, then we return an OrderedCollectionPage
    resp = {
        "@context": ap.COLLECTION_CTX,
        "type": ap.ActivityType.ORDERED_COLLECTION_PAGE.value,
        "id": BASE_URL + "/" + col_name + "?cursor=" + start_cursor,
        "totalItems": total_items,
        "partOf": BASE_URL + "/" + col_name,
        "orderedItems": data,
    }
    if len(data) == limit:
        resp["next"] = BASE_URL + "/" + col_name + "?cursor=" + next_page_cursor

    if first_page:
        return resp["first"]

    # XXX(tsileo): implements prev with prev=<first item cursor>?

    return resp
