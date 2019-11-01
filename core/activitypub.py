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
from flask import url_for
from little_boxes import activitypub as ap
from little_boxes import strtobool
from little_boxes.activitypub import _to_list
from little_boxes.activitypub import clean_activity
from little_boxes.activitypub import format_datetime
from little_boxes.backend import Backend
from little_boxes.errors import ActivityGoneError
from little_boxes.httpsig import HTTPSigAuth

from config import BASE_URL
from config import DB
from config import DEFAULT_CTX
from config import ID
from config import KEY
from config import ME
from config import USER_AGENT
from core.db import find_one_activity
from core.db import update_many_activities
from core.db import update_one_activity
from core.meta import Box
from core.meta import FollowStatus
from core.meta import MetaKey
from core.meta import by_object_id
from core.meta import by_remote_id
from core.meta import by_type
from core.meta import flag
from core.meta import inc
from core.meta import upsert
from core.remote import server
from core.tasks import Tasks
from utils import now

logger = logging.getLogger(__name__)

_NewMeta = Dict[str, Any]

SIG_AUTH = HTTPSigAuth(KEY)

MY_PERSON = ap.Person(**ME)

_LOCAL_NETLOC = urlparse(BASE_URL).netloc


def is_from_outbox(activity: ap.BaseActivity) -> bool:
    return activity.id.startswith(BASE_URL)


def is_local_url(url: str) -> bool:
    return urlparse(url).netloc == _LOCAL_NETLOC


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


def _actor_url(actor: ap.ActivityType) -> str:
    if isinstance(actor.url, dict):
        if actor.url.get("type") == ap.ActivityType.LINK.value:
            return actor.url["href"]

        raise ValueError(f"unkown actor url object type: {actor.url!r}")

    elif isinstance(actor.url, str):
        return actor.url

    # Return the actor ID if we cannot get the URL
    elif isinstance(actor.id, str):
        return actor.id

    else:
        raise ValueError(f"invalid actor URL: {actor.url!r}")


def _actor_hash(actor: ap.ActivityType, local: bool = False) -> str:
    """Used to know when to update the meta actor cache, like an "actor version"."""
    h = hashlib.new("sha1")
    h.update(actor.id.encode())
    h.update((actor.name or "").encode())
    h.update((actor.preferredUsername or "").encode())
    h.update((actor.summary or "").encode())
    h.update(_actor_url(actor).encode())
    key = actor.get_key()
    h.update(key.pubkey_pem.encode())
    h.update(key.key_id().encode())
    if isinstance(actor.icon, dict) and "url" in actor.icon:
        h.update(actor.icon["url"].encode())
    if local:
        # The local hash helps us detect when to send an Update
        if actor.attachment:
            for item in actor.attachment:
                h.update(item["name"].encode())
                h.update(item["value"].encode())
        h.update(("1" if actor.manuallyApprovesFollowers else "0").encode())
    return h.hexdigest()


def _is_local_reply(create: ap.Create) -> bool:
    for dest in _to_list(create.to or []):
        if dest.startswith(BASE_URL):
            return True

    for dest in _to_list(create.cc or []):
        if dest.startswith(BASE_URL):
            return True

    return False


def _meta(activity: ap.BaseActivity) -> _NewMeta:
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

    return {
        MetaKey.UNDO.value: False,
        MetaKey.DELETED.value: False,
        MetaKey.PUBLIC.value: is_public,
        MetaKey.SERVER.value: urlparse(activity.id).netloc,
        MetaKey.VISIBILITY.value: visibility.name,
        MetaKey.ACTOR_ID.value: actor_id,
        MetaKey.OBJECT_ID.value: object_id,
        MetaKey.OBJECT_VISIBILITY.value: object_visibility,
        MetaKey.POLL_ANSWER.value: False,
        MetaKey.PUBLISHED.value: activity.published if activity.published else now(),
    }


def save(box: Box, activity: ap.BaseActivity) -> None:
    """Custom helper for saving an activity to the DB."""
    # Set some "type"-related neta
    meta = _meta(activity)
    if box == Box.OUTBOX and activity.has_type(ap.ActivityType.FOLLOW):
        meta[MetaKey.FOLLOW_STATUS.value] = FollowStatus.WAITING.value
    elif activity.has_type(ap.ActivityType.CREATE):
        mentions = []
        obj = activity.get_object()
        for m in obj.get_mentions():
            mentions.append(m.href)
        hashtags = []
        for h in obj.get_hashtags():
            hashtags.append(h.name[1:])  # Strip the #
        meta.update(
            {MetaKey.MENTIONS.value: mentions, MetaKey.HASHTAGS.value: hashtags}
        )

    DB.activities.insert_one(
        {
            "box": box.value,
            "activity": activity.to_dict(),
            "type": _to_list(activity.type),
            "remote_id": activity.id,
            "meta": meta,
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

    # If the message is coming from a Pleroma relay, we process it as a possible reply for a stream activity
    if (
        actor.has_type(ap.ActivityType.APPLICATION)
        and actor.id.endswith("/relay")
        and activity.has_type(ap.ActivityType.ANNOUNCE)
        and not find_one_activity(
            {
                **by_object_id(activity.get_object_id()),
                **by_type(ap.ActivityType.CREATE),
            }
        )
        and not DB.replies.find_one(by_remote_id(activity.get_object_id()))
    ):
        Tasks.process_reply(activity.get_object_id())
        return

    # Hubzilla sends Update with the same ID as the actor, and it poisons the cache
    if (
        activity.has_type(ap.ActivityType.UPDATE)
        and activity.id == activity.get_object_id()
    ):
        # Start a task to update the cached actor
        Tasks.cache_actor(activity.id)
        return

    # Honk forwards activities in a Read, process them as replies
    if activity.has_type(ap.ActivityType.READ):
        Tasks.process_reply(activity.get_object_id())
        return

    # TODO(tsileo): support ignore from Honk

    # Hubzilla forwards activities in a Create, process them as possible replies
    if activity.has_type(ap.ActivityType.CREATE) and server(activity.id) != server(
        activity.get_object_id()
    ):
        Tasks.process_reply(activity.get_object_id())
        return

    if DB.activities.find_one({"box": Box.INBOX.value, "remote_id": activity.id}):
        # The activity is already in the inbox
        logger.info(f"received duplicate activity {activity!r}, dropping it")
        return

    save(Box.INBOX, activity)
    logger.info(f"spawning tasks for {activity!r}")
    if not activity.has_type([ap.ActivityType.DELETE, ap.ActivityType.UPDATE]):
        Tasks.cache_actor(activity.id)
    Tasks.process_new_activity(activity.id)
    Tasks.finish_post_to_inbox(activity.id)


def save_reply(activity: ap.BaseActivity, meta: Dict[str, Any] = {}) -> None:
    visibility = ap.get_visibility(activity)
    is_public = False
    if visibility in [ap.Visibility.PUBLIC, ap.Visibility.UNLISTED]:
        is_public = True

    published = activity.published if activity.published else now()
    DB.replies.insert_one(
        {
            "activity": activity.to_dict(),
            "type": _to_list(activity.type),
            "remote_id": activity.id,
            "meta": {
                "undo": False,
                "deleted": False,
                "public": is_public,
                "server": urlparse(activity.id).netloc,
                "visibility": visibility.name,
                "actor_id": activity.get_actor().id,
                MetaKey.PUBLISHED.value: published,
                **meta,
            },
        }
    )


def new_context(parent: Optional[ap.BaseActivity] = None) -> str:
    """`context` is here to group related activities, it's not meant to be resolved.
    We're just following the convention."""
    # Copy the context from the parent if any
    if parent and (parent.context or parent.conversation):
        if parent.context:
            if isinstance(parent.context, str):
                return parent.context
            elif isinstance(parent.context, dict) and parent.context.get("id"):
                return parent.context["id"]
        return parent.conversation

    # Generate a new context
    ctx_id = binascii.hexlify(os.urandom(12)).decode("utf-8")
    return urljoin(BASE_URL, f"/contexts/{ctx_id}")


def post_to_outbox(activity: ap.BaseActivity) -> str:
    if activity.has_type(ap.CREATE_TYPES):
        activity = activity.build_create()

    # Assign create a random ID
    obj_id = binascii.hexlify(os.urandom(12)).decode("utf-8")
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

    def ap_context(self) -> Any:
        return DEFAULT_CTX

    def base_url(self) -> str:
        return BASE_URL

    def debug_mode(self) -> bool:
        return strtobool(os.getenv("MICROBLOGPUB_DEBUG", "false"))

    def user_agent(self) -> str:
        """Setup a custom user agent."""
        return USER_AGENT

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
            # Check if we're looking for an object wrapped in a Create
            obj = DB.activities.find_one({"meta.object_id": iri, "type": "Create"})
            if obj:
                if obj["meta"]["deleted"]:
                    raise ActivityGoneError(f"{iri} is gone")
                cached_object = obj["meta"].get("object")
                if cached_object:
                    return cached_object

                embedded_object = obj["activity"]["object"]
                if isinstance(embedded_object, dict):
                    return embedded_object

            # TODO(tsileo): also check the REPLIES box

            # Check if it's cached because it's a follower
            # Remove extra info (like the key hash if any)
            cleaned_iri = iri.split("#")[0]
            actor = DB.activities.find_one(
                {"meta.actor_id": cleaned_iri, "meta.actor": {"$exists": True}}
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

        reply = DB.replies.find_one(by_remote_id(iri))
        if reply:
            return reply["activity"]

        # Fetch the URL via HTTP
        logger.info(f"dereference {iri} via HTTP")
        return super().fetch_iri(iri)

    def fetch_iri(self, iri: str, **kwargs: Any) -> ap.ObjectType:
        if not kwargs.pop("no_cache", False):
            # Fetch the activity by checking the local DB first
            data = self._fetch_iri(iri)
            logger.debug(f"_fetch_iri({iri!r}) == {data!r}")
        else:
            # Pass the SIG_AUTH to enable "authenticated fetch"
            data = super().fetch_iri(iri, auth=SIG_AUTH)
            logger.debug(f"fetch_iri({iri!r}) == {data!r}")

        return data


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
        "@context": DEFAULT_CTX,
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
                "@context": DEFAULT_CTX,
                "type": ap.ActivityType.ORDERED_COLLECTION_PAGE.value,
                "id": BASE_URL + "/" + col_name + "?cursor=" + cursor,
                "partOf": BASE_URL + "/" + col_name,
                "totalItems": 0,
                "orderedItems": [],
            }
        return {
            "@context": DEFAULT_CTX,
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
            "@context": DEFAULT_CTX,
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
        "@context": DEFAULT_CTX,
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
    if not ap._has_type(raw_doc["activity"]["type"], ap.ActivityType.CREATE.value):
        return raw_doc

    raw_doc["activity"]["object"]["replies"] = embed_collection(
        raw_doc.get("meta", {}).get(MetaKey.COUNT_REPLY.value, 0),
        f'{raw_doc["remote_id"]}/replies',
    )

    raw_doc["activity"]["object"]["likes"] = embed_collection(
        raw_doc.get("meta", {}).get(MetaKey.COUNT_LIKE.value, 0),
        f'{raw_doc["remote_id"]}/likes',
    )

    raw_doc["activity"]["object"]["shares"] = embed_collection(
        raw_doc.get("meta", {}).get(MetaKey.COUNT_BOOST.value, 0),
        f'{raw_doc["remote_id"]}/shares',
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


def _cache_actor_icon(actor: ap.BaseActivity) -> None:
    if actor.icon:
        if isinstance(actor.icon, dict) and "url" in actor.icon:
            Tasks.cache_actor_icon(actor.icon["url"], actor.id)
        else:
            logger.warning(f"failed to parse icon {actor.icon} for {actor!r}")


def update_cached_actor(actor: ap.BaseActivity) -> None:
    actor_hash = _actor_hash(actor)
    update_many_activities(
        {
            **flag(MetaKey.ACTOR_ID, actor.id),
            **flag(MetaKey.ACTOR_HASH, {"$ne": actor_hash}),
        },
        upsert(
            {MetaKey.ACTOR: actor.to_dict(embed=True), MetaKey.ACTOR_HASH: actor_hash}
        ),
    )
    update_many_activities(
        {
            **flag(MetaKey.OBJECT_ACTOR_ID, actor.id),
            **flag(MetaKey.OBJECT_ACTOR_HASH, {"$ne": actor_hash}),
        },
        upsert(
            {
                MetaKey.OBJECT_ACTOR: actor.to_dict(embed=True),
                MetaKey.OBJECT_ACTOR_HASH: actor_hash,
            }
        ),
    )
    DB.replies.update_many(
        {
            **flag(MetaKey.ACTOR_ID, actor.id),
            **flag(MetaKey.ACTOR_HASH, {"$ne": actor_hash}),
        },
        upsert(
            {MetaKey.ACTOR: actor.to_dict(embed=True), MetaKey.ACTOR_HASH: actor_hash}
        ),
    )
    # TODO(tsileo): Also update following (it's in the object)
    # DB.activities.update_many(
    #     {"meta.object_id": actor.id}, {"$set": {"meta.object": actor.to_dict(embed=True)}}
    # )
    _cache_actor_icon(actor)
    Tasks.cache_emojis(actor)


def handle_question_reply(create: ap.Create, question: ap.Question) -> None:
    choice = create.get_object().name

    # Ensure it's a valid choice
    if choice not in [c["name"] for c in question._data.get("oneOf", question.anyOf)]:
        logger.info("invalid choice")
        return

    # Hash the choice/answer (so we can use it as a key)
    answer_key = _answer_key(choice)

    is_single_choice = bool(question._data.get("oneOf", []))
    dup_query = {
        "activity.object.actor": create.get_actor().id,
        "meta.answer_to": question.id,
        **({} if is_single_choice else {"meta.poll_answer_choice": choice}),
    }

    print(f"dup_q={dup_query}")
    # Check for duplicate votes
    if DB.activities.find_one(dup_query):
        logger.info("duplicate response")
        return

    # Update the DB

    DB.activities.update_one(
        {**by_object_id(question.id), **by_type(ap.ActivityType.CREATE)},
        {
            "$inc": {
                "meta.question_replies": 1,
                f"meta.question_answers.{answer_key}": 1,
            }
        },
    )

    DB.activities.update_one(
        by_remote_id(create.id),
        {
            "$set": {
                "meta.poll_answer_to": question.id,
                "meta.poll_answer_choice": choice,
                "meta.stream": False,
                "meta.poll_answer": True,
            }
        },
    )

    return None


def handle_replies(create: ap.Create) -> None:
    """Go up to the root reply, store unknown replies in the `threads` DB and set the "meta.thread_root_parent"
    key to make it easy to query a whole thread."""
    in_reply_to = create.get_object().get_in_reply_to()
    if not in_reply_to:
        return

    reply = ap.fetch_remote_activity(in_reply_to)
    if reply.has_type(ap.ActivityType.CREATE):
        reply = reply.get_object()
    # FIXME(tsileo): can be a 403 too, in this case what to do? not error at least

    # Ensure the this is a local reply, of a question, with a direct "to" addressing
    if (
        reply.id.startswith(BASE_URL)
        and reply.has_type(ap.ActivityType.QUESTION.value)
        and _is_local_reply(create)
        and not create.is_public()
    ):
        return handle_question_reply(create, reply)
    elif (
        create.id.startswith(BASE_URL)
        and reply.has_type(ap.ActivityType.QUESTION.value)
        and not create.is_public()
    ):
        # Keep track of our own votes
        DB.activities.update_one(
            {"activity.object.id": reply.id, "box": "inbox"},
            {
                "$set": {
                    f"meta.poll_answers_sent.{_answer_key(create.get_object().name)}": True
                }
            },
        )
        # Mark our reply as a poll answers, to "hide" it from the UI
        update_one_activity(
            by_remote_id(create.id),
            upsert({MetaKey.POLL_ANSWER: True, MetaKey.POLL_ANSWER_TO: reply.id}),
        )
        return None

    in_reply_to_data = {MetaKey.IN_REPLY_TO: in_reply_to}
    # Update the activity to save some data about the reply
    if reply.get_actor().id == create.get_actor().id:
        in_reply_to_data.update({MetaKey.IN_REPLY_TO_SELF: True})
    else:
        in_reply_to_data.update(
            {MetaKey.IN_REPLY_TO_ACTOR: reply.get_actor().to_dict(embed=True)}
        )
    update_one_activity(by_remote_id(create.id), upsert(in_reply_to_data))

    # It's a regular reply, try to increment the reply counter
    creply = DB.activities.find_one_and_update(
        {**by_object_id(in_reply_to), **by_type(ap.ActivityType.CREATE)},
        inc(MetaKey.COUNT_REPLY, 1),
    )
    if not creply:
        # Maybe it's the reply of a reply?
        DB.replies.find_one_and_update(
            by_remote_id(in_reply_to), inc(MetaKey.COUNT_REPLY, 1)
        )

    # Spawn a task to process it (and determine if it needs to be saved)
    Tasks.process_reply(create.get_object_id())


def accept_follow(activity: ap.BaseActivity) -> str:
    actor_id = activity.get_actor().id
    accept = ap.Accept(
        actor=ID,
        context=new_context(activity),
        object={
            "type": "Follow",
            "id": activity.id,
            "object": activity.get_object_id(),
            "actor": actor_id,
        },
        to=[actor_id],
        published=now(),
    )
    update_one_activity(
        by_remote_id(activity.id),
        upsert({MetaKey.FOLLOW_STATUS: FollowStatus.ACCEPTED.value}),
    )
    return post_to_outbox(accept)
