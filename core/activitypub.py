import binascii
import hashlib
import logging
import os
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from urllib.parse import urljoin
from urllib.parse import urlparse

from bson.objectid import ObjectId
from cachetools import LRUCache
from flask import url_for
from little_boxes import activitypub as ap
from little_boxes import strtobool
from little_boxes.activitypub import _to_list
from little_boxes.activitypub import clean_activity
from little_boxes.activitypub import format_datetime
from little_boxes.backend import Backend
from little_boxes.errors import ActivityGoneError

from config import BASE_URL
from config import DB
from config import EXTRA_INBOXES
from config import ID
from config import ME
from config import USER_AGENT
from core.meta import Box
from core.tasks import Tasks

logger = logging.getLogger(__name__)

_NewMeta = Dict[str, Any]


ACTORS_CACHE = LRUCache(maxsize=256)
MY_PERSON = ap.Person(**ME)


def _remove_id(doc: ap.ObjectType) -> ap.ObjectType:
    """Helper for removing MongoDB's `_id` field."""
    doc = doc.copy()
    if "_id" in doc:
        del doc["_id"]
    return doc


def _answer_key(choice: str) -> str:
    h = hashlib.new("sha1")
    h.update(choice.encode())
    return h.hexdigest()


def _is_local_reply(create: ap.Create) -> bool:
    for dest in _to_list(create.to or []):
        if dest.startswith(BASE_URL):
            return True

    for dest in _to_list(create.cc or []):
        if dest.startswith(BASE_URL):
            return True

    return False


def save(box: Box, activity: ap.BaseActivity) -> None:
    """Custom helper for saving an activity to the DB."""
    visibility = ap.get_visibility(activity)
    is_public = False
    if visibility in [ap.Visibility.PUBLIC, ap.Visibility.UNLISTED]:
        is_public = True

    object_id = None
    try:
        object_id = activity.get_object_id()
    except Exception:  # TODO(tsileo): should be ValueError, but replies trigger a KeyError on object
        pass

    object_visibility = None
    if activity.has_type(
        [ap.ActivityType.CREATE, ap.ActivityType.ANNOUNCE, ap.ActivityType.LIKE]
    ):
        object_visibility = ap.get_visibility(activity.get_object()).name

    actor_id = activity.get_actor().id

    DB.activities.insert_one(
        {
            "box": box.value,
            "activity": activity.to_dict(),
            "type": _to_list(activity.type),
            "remote_id": activity.id,
            "meta": {
                "undo": False,
                "deleted": False,
                "public": is_public,
                "server": urlparse(activity.id).netloc,
                "visibility": visibility.name,
                "actor_id": actor_id,
                "object_id": object_id,
                "object_visibility": object_visibility,
                "poll_answer": False,
            },
        }
    )


def outbox_is_blocked(actor_id: str) -> bool:
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


def activity_url(item_id: str) -> str:
    return urljoin(BASE_URL, url_for("outbox_detail", item_id=item_id))


def post_to_inbox(activity: ap.BaseActivity) -> None:
    # Check for Block activity
    actor = activity.get_actor()
    if outbox_is_blocked(actor.id):
        logger.info(
            f"actor {actor!r} is blocked, dropping the received activity {activity!r}"
        )
        return

    if DB.activities.find_one({"box": Box.INBOX.value, "remote_id": activity.id}):
        # The activity is already in the inbox
        logger.info(f"received duplicate activity {activity!r}, dropping it")
        return

    save(Box.INBOX, activity)
    Tasks.process_new_activity(activity.id)

    logger.info(f"spawning task for {activity!r}")
    Tasks.finish_post_to_inbox(activity.id)


def post_to_outbox(activity: ap.BaseActivity) -> str:
    if activity.has_type(ap.CREATE_TYPES):
        activity = activity.build_create()

    # Assign create a random ID
    obj_id = binascii.hexlify(os.urandom(8)).decode("utf-8")
    uri = activity_url(obj_id)
    activity._data["id"] = uri
    if activity.has_type(ap.ActivityType.CREATE):
        activity._data["object"]["id"] = urljoin(
            BASE_URL, url_for("outbox_activity", item_id=obj_id)
        )
        activity._data["object"]["url"] = urljoin(
            BASE_URL, url_for("note_by_id", note_id=obj_id)
        )
        activity.reset_object_cache()

    save(Box.OUTBOX, activity)
    Tasks.cache_actor(activity.id)
    Tasks.finish_post_to_outbox(activity.id)
    return activity.id


class MicroblogPubBackend(Backend):
    """Implements a Little Boxes backend, backed by MongoDB."""

    def base_url(self) -> str:
        return BASE_URL

    def debug_mode(self) -> bool:
        return strtobool(os.getenv("MICROBLOGPUB_DEBUG", "false"))

    def user_agent(self) -> str:
        """Setup a custom user agent."""
        return USER_AGENT

    def extra_inboxes(self) -> List[str]:
        return EXTRA_INBOXES

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

    def _fetch_iri(self, iri: str) -> ap.ObjectType:  # noqa: C901
        # Shortcut if the instance actor is fetched
        if iri == ME["id"]:
            return ME

        # Internal collecitons handling
        # Followers
        if iri == MY_PERSON.followers:
            followers = []
            for data in DB.activities.find(
                {
                    "box": Box.INBOX.value,
                    "type": ap.ActivityType.FOLLOW.value,
                    "meta.undo": False,
                }
            ):
                followers.append(data["meta"]["actor_id"])
            return {"type": "Collection", "items": followers}

        # Following
        if iri == MY_PERSON.following:
            following = []
            for data in DB.activities.find(
                {
                    "box": Box.OUTBOX.value,
                    "type": ap.ActivityType.FOLLOW.value,
                    "meta.undo": False,
                }
            ):
                following.append(data["meta"]["object_id"])
            return {"type": "Collection", "items": following}

        # TODO(tsileo): handle the liked collection too

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
            obj = DB.activities.find_one({"meta.object_id": iri, "type": "Create"})
            if obj:
                if obj["meta"]["deleted"]:
                    raise ActivityGoneError(f"{iri} is gone")
                return obj["meta"].get("object") or obj["activity"]["object"]

            # Check if it's cached because it's a follower
            # Remove extra info (like the key hash if any)
            cleaned_iri = iri.split("#")[0]
            actor = DB.activities.find_one(
                {
                    "meta.actor_id": cleaned_iri,
                    "type": ap.ActivityType.FOLLOW.value,
                    "meta.undo": False,
                }
            )

            # "type" check is here to skip old metadata for "old/buggy" followers
            if (
                actor
                and actor["meta"].get("actor")
                and "type" in actor["meta"]["actor"]
            ):
                return actor["meta"]["actor"]

            # Check if it's cached because it's a following
            actor2 = DB.activities.find_one(
                {
                    "meta.object_id": cleaned_iri,
                    "type": ap.ActivityType.FOLLOW.value,
                    "meta.undo": False,
                }
            )
            if (
                actor2
                and actor2["meta"].get("object")
                and "type" in actor2["meta"]["object"]
            ):
                return actor2["meta"]["object"]

        # Fetch the URL via HTTP
        logger.info(f"dereference {iri} via HTTP")
        return super().fetch_iri(iri)

    def fetch_iri(self, iri: str, no_cache=False) -> ap.ObjectType:
        if not no_cache:
            # Fetch the activity by checking the local DB first
            data = self._fetch_iri(iri)
        else:
            data = super().fetch_iri(iri)

        logger.debug(f"_fetch_iri({iri!r}) == {data!r}")

        return data

    def set_post_to_remote_inbox(self, cb):
        self.post_to_remote_inbox_cb = cb

    def _handle_replies_delete(
        self, as_actor: ap.Person, in_reply_to: Optional[str]
    ) -> None:
        if not in_reply_to:
            pass

        DB.activities.update_one(
            {"activity.object.id": in_reply_to},
            {"$inc": {"meta.count_reply": -1, "meta.count_direct_reply": -1}},
        )

    def _process_question_reply(self, create: ap.Create, question: ap.Question) -> None:
        choice = create.get_object().name

        # Ensure it's a valid choice
        if choice not in [
            c["name"] for c in question._data.get("oneOf", question.anyOf)
        ]:
            logger.info("invalid choice")
            return

        # Check for duplicate votes
        if DB.activities.find_one(
            {
                "activity.object.actor": create.get_actor().id,
                "meta.answer_to": question.id,
            }
        ):
            logger.info("duplicate response")
            return

        # Update the DB
        answer_key = _answer_key(choice)

        DB.activities.update_one(
            {"activity.object.id": question.id},
            {
                "$inc": {
                    "meta.question_replies": 1,
                    f"meta.question_answers.{answer_key}": 1,
                }
            },
        )

        DB.activities.update_one(
            {"remote_id": create.id},
            {
                "$set": {
                    "meta.answer_to": question.id,
                    "meta.stream": False,
                    "meta.poll_answer": True,
                }
            },
        )

        return None

    def _handle_replies(self, as_actor: ap.Person, create: ap.Create) -> None:
        """Go up to the root reply, store unknown replies in the `threads` DB and set the "meta.thread_root_parent"
        key to make it easy to query a whole thread."""
        in_reply_to = create.get_object().get_in_reply_to()
        if not in_reply_to:
            return

        new_threads = []
        root_reply = in_reply_to
        reply = ap.fetch_remote_activity(root_reply)

        # Ensure the this is a local reply, of a question, with a direct "to" addressing
        if (
            reply.id.startswith(BASE_URL)
            and reply.has_type(ap.ActivityType.QUESTION.value)
            and _is_local_reply(create)
            and not create.is_public()
        ):
            return self._process_question_reply(create, reply)
        elif (
            create.id.startswith(BASE_URL)
            and reply.has_type(ap.ActivityType.QUESTION.value)
            and not create.is_public()
        ):
            # Keep track of our own votes
            DB.activities.update_one(
                {"activity.object.id": reply.id, "box": "inbox"},
                {"$set": {"meta.voted_for": create.get_object().name}},
            )
            return None

        print(f"processing {create!r} and incrementing {in_reply_to}")
        creply = DB.activities.find_one_and_update(
            {"activity.object.id": in_reply_to},
            {"$inc": {"meta.count_reply": 1, "meta.count_direct_reply": 1}},
        )
        if not creply:
            # It means the activity is not in the inbox, and not in the outbox, we want to save it
            save(Box.REPLIES, reply)
            new_threads.append(reply.id)
            # TODO(tsileo): parses the replies collection and import the replies?

        while reply is not None:
            in_reply_to = reply.get_in_reply_to()
            if not in_reply_to:
                break
            root_reply = in_reply_to
            reply = ap.fetch_remote_activity(root_reply)
            q = {"activity.object.id": root_reply}
            if not DB.activities.count(q):
                save(Box.REPLIES, reply)
                new_threads.append(reply.id)

        DB.activities.update_one(
            {"remote_id": create.id}, {"$set": {"meta.thread_root_parent": root_reply}}
        )
        DB.activities.update(
            {"box": Box.REPLIES.value, "remote_id": {"$in": new_threads}},
            {"$set": {"meta.thread_root_parent": root_reply}},
        )


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


def _add_answers_to_question(raw_doc: Dict[str, Any]) -> None:
    activity = raw_doc["activity"]
    if (
        ap._has_type(activity["type"], ap.ActivityType.CREATE)
        and "object" in activity
        and ap._has_type(activity["object"]["type"], ap.ActivityType.QUESTION)
    ):
        for choice in activity["object"].get("oneOf", activity["object"].get("anyOf")):
            choice["replies"] = {
                "type": ap.ActivityType.COLLECTION.value,
                "totalItems": raw_doc["meta"]
                .get("question_answers", {})
                .get(_answer_key(choice["name"]), 0),
            }
        now = datetime.now(timezone.utc)
        if format_datetime(now) >= activity["object"]["endTime"]:
            activity["object"]["closed"] = activity["object"]["endTime"]


def add_extra_collection(raw_doc: Dict[str, Any]) -> Dict[str, Any]:
    if raw_doc["activity"]["type"] != ap.ActivityType.CREATE.value:
        return raw_doc

    raw_doc["activity"]["object"]["replies"] = embed_collection(
        raw_doc.get("meta", {}).get("count_direct_reply", 0),
        f'{raw_doc["remote_id"]}/replies',
    )

    raw_doc["activity"]["object"]["likes"] = embed_collection(
        raw_doc.get("meta", {}).get("count_like", 0), f'{raw_doc["remote_id"]}/likes'
    )

    raw_doc["activity"]["object"]["shares"] = embed_collection(
        raw_doc.get("meta", {}).get("count_boost", 0), f'{raw_doc["remote_id"]}/shares'
    )

    return raw_doc


def remove_context(activity: Dict[str, Any]) -> Dict[str, Any]:
    if "@context" in activity:
        del activity["@context"]
    return activity


def activity_from_doc(raw_doc: Dict[str, Any], embed: bool = False) -> Dict[str, Any]:
    raw_doc = add_extra_collection(raw_doc)
    activity = clean_activity(raw_doc["activity"])

    # Handle Questions
    # TODO(tsileo): what about object embedded by ID/URL?
    _add_answers_to_question(raw_doc)
    if embed:
        return remove_context(activity)
    return activity
