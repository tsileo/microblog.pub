import logging
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

from bson.objectid import ObjectId
from feedgen.feed import FeedGenerator
from html2text import html2text

import tasks
from config import BASE_URL
from config import DB
from config import ID
from config import ME
from config import USER_AGENT
from config import USERNAME
from little_boxes import activitypub as ap
from little_boxes.backend import Backend
from little_boxes.collection import parse_collection as ap_parse_collection
from little_boxes.errors import Error

logger = logging.getLogger(__name__)


def _remove_id(doc: ap.ObjectType) -> ap.ObjectType:
    """Helper for removing MongoDB's `_id` field."""
    doc = doc.copy()
    if "_id" in doc:
        del (doc["_id"])
    return doc


def _to_list(data: Union[List[Any], Any]) -> List[Any]:
    """Helper to convert fields that can be either an object or a list of objects to a list of object."""
    if isinstance(data, list):
        return data
    return [data]


def ensure_it_is_me(f):
    """Method decorator used to track the events fired during tests."""

    def wrapper(*args, **kwargs):
        if args[1].id != ME["id"]:
            raise Error("unexpected actor")
        return f(*args, **kwargs)

    return wrapper


class MicroblogPubBackend(Backend):
    def user_agent(self) -> str:
        return USER_AGENT

    def base_url(self) -> str:
        return BASE_URL

    def activity_url(self, obj_id):
        return f"{BASE_URL}/outbox/{obj_id}"

    @ensure_it_is_me
    def outbox_new(self, as_actor: ap.Person, activity: ap.BaseActivity) -> None:
        DB.outbox.insert_one(
            {
                "activity": activity.to_dict(),
                "type": activity.type,
                "remote_id": activity.id,
                "meta": {"undo": False, "deleted": False},
            }
        )

    @ensure_it_is_me
    def outbox_is_blocked(self, as_actor: ap.Person, actor_id: str) -> bool:
        return bool(
            DB.outbox.find_one(
                {
                    "type": ap.ActivityType.BLOCK.value,
                    "activity.object": actor_id,
                    "meta.undo": False,
                }
            )
        )

    def fetch_iri(self, iri: str) -> ap.ObjectType:
        if iri == ME["id"]:
            return ME

        # Check if the activity is owned by this server
        if iri.startswith(BASE_URL):
            is_a_note = False
            if iri.endswith("/activity"):
                iri = iri.replace("/activity", "")
                is_a_note = True
            data = DB.outbox.find_one({"remote_id": iri})
            if data:
                if is_a_note:
                    return data["activity"]["object"]
                return data["activity"]
        else:
            # Check if the activity is stored in the inbox
            data = DB.inbox.find_one({"remote_id": iri})
            if data:
                return data["activity"]

        # Fetch the URL via HTTP
        return super().fetch_iri(iri)

    @ensure_it_is_me
    def inbox_check_duplicate(self, as_actor: ap.Person, iri: str) -> bool:
        return bool(DB.inbox.find_one({"remote_id": iri}))

    @ensure_it_is_me
    def inbox_new(self, as_actor: ap.Person, activity: ap.BaseActivity) -> None:
        DB.inbox.insert_one(
            {
                "activity": activity.to_dict(),
                "type": activity.type,
                "remote_id": activity.id,
                "meta": {"undo": False, "deleted": False},
            }
        )

    @ensure_it_is_me
    def post_to_remote_inbox(self, as_actor: ap.Person, payload: str, to: str) -> None:
        tasks.post_to_inbox.delay(payload, to)

    @ensure_it_is_me
    def new_follower(self, as_actor: ap.Person, follow: ap.Follow) -> None:
        remote_actor = follow.get_actor().id

        if DB.followers.find({"remote_actor": remote_actor}).count() == 0:
            DB.followers.insert_one({"remote_actor": remote_actor})

    @ensure_it_is_me
    def undo_new_follower(self, as_actor: ap.Person, follow: ap.Follow) -> None:
        # TODO(tsileo): update the follow to set undo
        DB.followers.delete_one({"remote_actor": follow.get_actor().id})

    @ensure_it_is_me
    def undo_new_following(self, as_actor: ap.Person, follow: ap.Follow) -> None:
        # TODO(tsileo): update the follow to set undo
        DB.following.delete_one({"remote_actor": follow.get_object().id})

    @ensure_it_is_me
    def new_following(self, as_actor: ap.Person, follow: ap.Follow) -> None:
        remote_actor = follow.get_object().id
        if DB.following.find({"remote_actor": remote_actor}).count() == 0:
            DB.following.insert_one({"remote_actor": remote_actor})

    @ensure_it_is_me
    def inbox_like(self, as_actor: ap.Person, like: ap.Like) -> None:
        obj = like.get_object()
        # Update the meta counter if the object is published by the server
        DB.outbox.update_one(
            {"activity.object.id": obj.id}, {"$inc": {"meta.count_like": 1}}
        )

    @ensure_it_is_me
    def inbox_undo_like(self, as_actor: ap.Person, like: ap.Like) -> None:
        obj = like.get_object()
        # Update the meta counter if the object is published by the server
        DB.outbox.update_one(
            {"activity.object.id": obj.id}, {"$inc": {"meta.count_like": -1}}
        )

    @ensure_it_is_me
    def outbox_like(self, as_actor: ap.Person, like: ap.Like) -> None:
        obj = like.get_object()
        # Unlikely, but an actor can like it's own post
        DB.outbox.update_one(
            {"activity.object.id": obj.id}, {"$inc": {"meta.count_like": 1}}
        )

        # Keep track of the like we just performed
        DB.inbox.update_one(
            {"activity.object.id": obj.id}, {"$set": {"meta.liked": like.id}}
        )

    @ensure_it_is_me
    def outbox_undo_like(self, as_actor: ap.Person, like: ap.Like) -> None:
        obj = like.get_object()
        # Unlikely, but an actor can like it's own post
        DB.outbox.update_one(
            {"activity.object.id": obj.id}, {"$inc": {"meta.count_like": -1}}
        )

        DB.inbox.update_one(
            {"activity.object.id": obj.id}, {"$set": {"meta.liked": False}}
        )

    @ensure_it_is_me
    def inbox_announce(self, as_actor: ap.Person, announce: ap.Announce) -> None:
        if isinstance(announce._data["object"], str) and not announce._data[
            "object"
        ].startswith("http"):
            # TODO(tsileo): actually drop it without storing it and better logging, also move the check somewhere else
            logger.warn(
                f'received an Annouce referencing an OStatus notice ({announce._data["object"]}), dropping the message'
            )
            return
        # FIXME(tsileo):  Save/cache the object, and make it part of the stream so we can fetch it
        if isinstance(announce._data["object"], str):
            obj_iri = announce._data["object"]
        else:
            obj_iri = self.get_object().id

        DB.outbox.update_one(
            {"activity.object.id": obj_iri}, {"$inc": {"meta.count_boost": 1}}
        )

    @ensure_it_is_me
    def inbox_undo_announce(self, as_actor: ap.Person, announce: ap.Announce) -> None:
        obj = announce.get_object()
        # Update the meta counter if the object is published by the server
        DB.outbox.update_one(
            {"activity.object.id": obj.id}, {"$inc": {"meta.count_boost": -1}}
        )

    @ensure_it_is_me
    def outbox_announce(self, as_actor: ap.Person, announce: ap.Announce) -> None:
        obj = announce.get_object()
        DB.inbox.update_one(
            {"activity.object.id": obj.id}, {"$set": {"meta.boosted": announce.id}}
        )

    @ensure_it_is_me
    def outbox_undo_announce(self, as_actor: ap.Person, announce: ap.Announce) -> None:
        obj = announce.get_object()
        DB.inbox.update_one(
            {"activity.object.id": obj.id}, {"$set": {"meta.boosted": False}}
        )

    @ensure_it_is_me
    def inbox_delete(self, as_actor: ap.Person, delete: ap.Delete) -> None:
        DB.inbox.update_one(
            {"activity.object.id": delete.get_object().id},
            {"$set": {"meta.deleted": True}},
        )
        obj = delete.get_object()
        if obj.ACTIVITY_TYPE != ap.ActivityType.NOTE:
            obj = self.fetch_iri(delete.get_object().id)
        self._handle_replies_delete(as_actor, obj)

        # FIXME(tsileo): handle threads
        # obj = delete._get_actual_object()
        # if obj.type_enum == ActivityType.NOTE:
        #    obj._delete_from_threads()

        # TODO(tsileo): also purge the cache if it's a reply of a published activity

    @ensure_it_is_me
    def outbox_delete(self, as_actor: ap.Person, delete: ap.Delete) -> None:
        DB.outbox.update_one(
            {"activity.object.id": delete.get_object().id},
            {"$set": {"meta.deleted": True}},
        )

    @ensure_it_is_me
    def inbox_update(self, as_actor: ap.Person, update: ap.Update) -> None:
        obj = update.get_object()
        if obj.ACTIVITY_TYPE == ap.ActivityType.NOTE:
            DB.inbox.update_one(
                {"activity.object.id": obj.id},
                {"$set": {"activity.object": obj.to_dict()}},
            )
            return

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
        DB.outbox.update_one({"activity.object.id": obj["id"]}, update)
        # FIXME(tsileo): should send an Update (but not a partial one, to all the note's recipients
        # (create a new Update with the result of the update, and send it without saving it?)

    @ensure_it_is_me
    def outbox_create(self, as_actor: ap.Person, create: ap.Create) -> None:
        self._handle_replies(as_actor, create)

    @ensure_it_is_me
    def inbox_create(self, as_actor: ap.Person, create: ap.Create) -> None:
        self._handle_replies(as_actor, create)

    @ensure_it_is_me
    def _handle_replies_delete(self, as_actor: ap.Person, note: ap.Create) -> None:
        in_reply_to = note.inReplyTo
        if not in_reply_to:
            pass

        if not DB.inbox.find_one_and_update(
            {"activity.object.id": in_reply_to},
            {"$inc": {"meta.count_reply": -1, "meta.count_direct_reply": -1}},
        ):
            DB.outbox.update_one(
                {"activity.object.id": in_reply_to},
                {"$inc": {"meta.count_reply": -1, "meta.count_direct_reply": -1}},
            )

    @ensure_it_is_me
    def _handle_replies(self, as_actor: ap.Person, create: ap.Create) -> None:
        in_reply_to = create.get_object().inReplyTo
        if not in_reply_to:
            pass

        if not DB.inbox.find_one_and_update(
            {"activity.object.id": in_reply_to},
            {"$inc": {"meta.count_reply": 1, "meta.count_direct_reply": 1}},
        ):
            DB.outbox.update_one(
                {"activity.object.id": in_reply_to},
                {"$inc": {"meta.count_reply": 1, "meta.count_direct_reply": 1}},
            )


def gen_feed():
    fg = FeedGenerator()
    fg.id(f"{ID}")
    fg.title(f"{USERNAME} notes")
    fg.author({"name": USERNAME, "email": "t@a4.io"})
    fg.link(href=ID, rel="alternate")
    fg.description(f"{USERNAME} notes")
    fg.logo(ME.get("icon", {}).get("url"))
    fg.language("en")
    for item in DB.outbox.find({"type": "Create"}, limit=50):
        fe = fg.add_entry()
        fe.id(item["activity"]["object"].get("url"))
        fe.link(href=item["activity"]["object"].get("url"))
        fe.title(item["activity"]["object"]["content"])
        fe.description(item["activity"]["object"]["content"])
    return fg


def json_feed(path: str) -> Dict[str, Any]:
    """JSON Feed (https://jsonfeed.org/) document."""
    data = []
    for item in DB.outbox.find({"type": "Create"}, limit=50):
        data.append(
            {
                "id": item["id"],
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
    data = []
    cursor = None

    q: Dict[str, Any] = {"type": "Create", "meta.deleted": False}
    if request_cursor:
        q["_id"] = {"$lt": request_cursor}

    for item in DB.inbox.find(q, limit=50).sort("_id", -1):
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


def parse_collection(
    payload: Optional[Dict[str, Any]] = None, url: Optional[str] = None
) -> List[str]:
    """Resolve/fetch a `Collection`/`OrderedCollection`."""
    # Resolve internal collections via MongoDB directly
    if url == ID + "/followers":
        return [doc["remote_actor"] for doc in DB.followers.find()]
    elif url == ID + "/following":
        return [doc["remote_actor"] for doc in DB.following.find()]

    # Go through all the pages
    return ap_parse_collection(payload, url)


def embed_collection(total_items, first_page_id):
    return {
        "type": ap.ActivityType.ORDERED_COLLECTION.value,
        "totalItems": total_items,
        "first": f"{first_page_id}?page=first",
        "id": first_page_id,
    }


def build_ordered_collection(
    col, q=None, cursor=None, map_func=None, limit=50, col_name=None, first_page=False
):
    col_name = col_name or col.name
    if q is None:
        q = {}

    if cursor:
        q["_id"] = {"$lt": ObjectId(cursor)}
    data = list(col.find(q, limit=limit).sort("_id", -1))

    if not data:
        return {
            "id": BASE_URL + "/" + col_name,
            "totalItems": 0,
            "type": ap.ActivityType.ORDERED_COLLECTION.value,
            "orederedItems": [],
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
