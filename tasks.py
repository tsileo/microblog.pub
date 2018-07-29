import json
import logging
import os
import random

import requests
from celery import Celery
from little_boxes import activitypub as ap
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import ActivityNotFoundError
from little_boxes.errors import NotAnActivityError
from little_boxes.httpsig import HTTPSigAuth
from little_boxes.linked_data_sig import generate_signature
from requests.exceptions import HTTPError

import activitypub
from activitypub import Box
from config import DB
from config import HEADERS
from config import ME
from config import ID
from config import KEY
from config import MEDIA_CACHE
from config import USER_AGENT
from utils import opengraph
from utils.media import Kind

log = logging.getLogger(__name__)
app = Celery(
    "tasks", broker=os.getenv("MICROBLOGPUB_AMQP_BROKER", "pyamqp://guest@localhost//")
)
SigAuth = HTTPSigAuth(KEY)


back = activitypub.MicroblogPubBackend()
ap.use_backend(back)


def save_cb(box: Box, iri: str) -> None:
    if box == Box.INBOX:
        process_new_activity.delay(iri)
    else:
        cache_actor.delay(iri)


back.set_save_cb(save_cb)


MY_PERSON = ap.Person(**ME)


@app.task(bind=True, max_retries=12)  # noqa: C901
def process_new_activity(self, iri: str) -> None:
    """Process an activity received in the inbox"""
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")

        # Is the activity expected?
        # following = ap.get_backend().following()
        should_forward = False
        should_delete = False

        tag_stream = False
        if activity.has_type(ap.ActivityType.ANNOUNCE):
            try:
                activity.get_object()
                tag_stream = True
            except NotAnActivityError:
                # Most likely on OStatus notice
                tag_stream = False
                should_delete = True

        elif activity.has_type(ap.ActivityType.CREATE):
            note = activity.get_object()
            # Make the note part of the stream if it's not a reply, or if it's a local reply
            if not note.inReplyTo or note.inReplyTo.startswith(ID):
                tag_stream = True

            if note.inReplyTo:
                try:
                    reply = ap.fetch_remote_activity(note.inReplyTo)
                    if (
                        reply.id.startswith(ID) or reply.has_mention(ID)
                    ) and activity.is_public():
                        # The reply is public "local reply", forward the reply (i.e. the original activity) to the
                        # original recipients
                        should_forward = True
                except NotAnActivityError:
                    # Most likely a reply to an OStatus notce
                    should_delete = True

            # (partial) Ghost replies handling
            # [X] This is the first time the server has seen this Activity.
            should_forward = False
            local_followers = ID + "/followers"
            for field in ["to", "cc"]:
                if field in activity._data:
                    if local_followers in activity._data[field]:
                        # [X] The values of to, cc, and/or audience contain a Collection owned by the server.
                        should_forward = True

            # [X] The values of inReplyTo, object, target and/or tag are objects owned by the server
            if not (note.inReplyTo and note.inReplyTo.startswith(ID)):
                should_forward = False

        elif activity.has_type(ap.ActivityType.DELETE):
            note = DB.activities.find_one(
                {"activity.object.id": activity.get_object().id}
            )
            if note and note["meta"].get("forwarded", False):
                # If the activity was originally forwarded, forward the delete too
                should_forward = True

        if should_forward:
            log.info(f"will forward {activity!r} to followers")
            activity.forward(back.followers_as_recipients())

        if should_delete:
            log.info(f"will soft delete {activity!r}")

        log.info(f"{iri} tag_stream={tag_stream}")
        DB.activities.update_one(
            {"remote_id": activity.id},
            {
                "$set": {
                    "meta.stream": tag_stream,
                    "meta.forwarded": should_forward,
                    "meta.deleted": should_delete,
                }
            },
        )

        log.info(f"new activity {iri} processed")
        if not should_delete:
            cache_actor.delay(iri)
    except (ActivityGoneError, ActivityNotFoundError):
        log.exception(f"dropping activity {iri}, skip processing")
    except Exception as err:
        log.exception(f"failed to process new activity {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


@app.task(bind=True, max_retries=12)  # noqa: C901
def fetch_og_metadata(self, iri: str) -> None:
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")
        if activity.has_type(ap.ActivityType.CREATE):
            note = activity.get_object()
            links = opengraph.links_from_note(note.to_dict())
            og_metadata = opengraph.fetch_og_metadata(USER_AGENT, links)
            for og in og_metadata:
                if not og.get("image"):
                    continue
                MEDIA_CACHE.cache_og_image(og["image"])

            log.debug(f"OG metadata {og_metadata!r}")
            DB.activities.update_one(
                {"remote_id": iri}, {"$set": {"meta.og_metadata": og_metadata}}
            )

        log.info(f"OG metadata fetched for {iri}")
    except (ActivityGoneError, ActivityNotFoundError):
        log.exception(f"dropping activity {iri}, skip OG metedata")
    except requests.exceptions.HTTPError as http_err:
        if 400 <= http_err.response.status_code < 500:
            log.exception("bad request, no retry")
            return
        log.exception("failed to fetch OG metadata")
        self.retry(
            exc=http_err, countdown=int(random.uniform(2, 4) ** self.request.retries)
        )
    except Exception as err:
        log.exception(f"failed to fetch OG metadata for {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


@app.task(bind=True, max_retries=12)
def cache_actor(self, iri: str, also_cache_attachments: bool = True) -> None:
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")

        if activity.has_type(ap.ActivityType.CREATE):
            fetch_og_metadata.delay(iri)

        actor = activity.get_actor()

        cache_actor_with_inbox = False
        if activity.has_type(ap.ActivityType.FOLLOW):
            if actor.id != ID:
                # It's a Follow from the Inbox
                cache_actor_with_inbox = True
            else:
                # It's a new following, cache the "object" (which is the actor we follow)
                DB.activities.update_one(
                    {"remote_id": iri},
                    {
                        "$set": {
                            "meta.object": activitypub._actor_to_meta(
                                activity.get_object()
                            )
                        }
                    },
                )

        # Cache the actor info
        DB.activities.update_one(
            {"remote_id": iri},
            {
                "$set": {
                    "meta.actor": activitypub._actor_to_meta(
                        actor, cache_actor_with_inbox
                    )
                }
            },
        )

        log.info(f"actor cached for {iri}")
        if also_cache_attachments:
            cache_attachments.delay(iri)

    except (ActivityGoneError, ActivityNotFoundError):
        DB.activities.update_one({"remote_id": iri}, {"$set": {"meta.deleted": True}})
        log.exception(f"flagging activity {iri} as deleted, no actor caching")
    except Exception as err:
        log.exception(f"failed to cache actor for {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


@app.task(bind=True, max_retries=12)
def cache_attachments(self, iri: str) -> None:
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")
        # Generates thumbnails for the actor's icon and the attachments if any

        actor = activity.get_actor()

        # Update the cached actor
        DB.actors.update_one(
            {"remote_id": iri},
            {"$set": {"remote_id": iri, "data": actor.to_dict(embed=True)}},
            upsert=True,
        )

        if actor.icon:
            MEDIA_CACHE.cache(actor.icon["url"], Kind.ACTOR_ICON)

        if activity.has_type(ap.ActivityType.CREATE):
            for attachment in activity.get_object()._data.get("attachment", []):
                if (
                    attachment.get("mediaType", "").startswith("image/")
                    or attachment.get("type") == ap.ActivityType.IMAGE.value
                ):
                    try:
                        MEDIA_CACHE.cache(attachment["url"], Kind.ATTACHMENT)
                    except ValueError:
                        log.exception(f"failed to cache {attachment}")

        log.info(f"attachments cached for {iri}")

    except (ActivityGoneError, ActivityNotFoundError):
        log.exception(f"dropping activity {iri}, no attachment caching")
    except Exception as err:
        log.exception(f"failed to cache attachments for {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


def post_to_inbox(activity: ap.BaseActivity) -> None:
    # Check for Block activity
    actor = activity.get_actor()
    if back.outbox_is_blocked(MY_PERSON, actor.id):
        log.info(
            f"actor {actor!r} is blocked, dropping the received activity {activity!r}"
        )
    return

    if back.inbox_check_duplicate(MY_PERSON, activity.id):
        # The activity is already in the inbox
        log.info(f"received duplicate activity {activity!r}, dropping it")

    back.save(Box.INBOX, activity)
    finish_post_to_inbox.delay(activity.id)


@app.task(bind=True, max_retries=12)  # noqa: C901
def finish_post_to_inbox(self, iri: str) -> None:
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")

        if activity.has_type(ap.ActivityType.DELETE):
            back.inbox_delete(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.UPDATE):
            back.inbox_update(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.CREATE):
            back.inbox_create(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.ANNOUNCE):
            back.inbox_announce(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.LIKE):
            back.inbox_like(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.FOLLOW):
            # Reply to a Follow with an Accept
            accept = ap.Accept(actor=ID, object=activity.to_dict(embed=True))
            post_to_outbox(accept)
        elif activity.has_type(ap.ActivityType.UNDO):
            obj = activity.get_object()
            if obj.has_type(ap.ActivityType.LIKE):
                back.inbox_undo_like(MY_PERSON, obj)
            elif obj.has_type(ap.ActivityType.ANNOUNCE):
                back.inbox_undo_announce(MY_PERSON, obj)
            elif obj.has_type(ap.ActivityType.FOLLOW):
                back.undo_new_follower(MY_PERSON, obj)

    except Exception as err:
        log.exception(f"failed to cache attachments for {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


def post_to_outbox(activity: ap.BaseActivity) -> str:
    if activity.has_type(ap.CREATE_TYPES):
        activity = activity.build_create()

    # Assign create a random ID
    obj_id = back.random_object_id()
    activity.set_id(back.activity_url(obj_id), obj_id)

    back.save(Box.OUTBOX, activity)
    finish_post_to_outbox.delay(activity.id)
    return activity.id


@app.task(bind=True, max_retries=12)  # noqa:C901
def finish_post_to_outbox(self, iri: str) -> None:
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")

        if activity.has_type(ap.ActivityType.DELETE):
            back.outbox_delete(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.UPDATE):
            back.outbox_update(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.CREATE):
            back.outbox_create(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.ANNOUNCE):
            back.outbox_announce(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.LIKE):
            back.outbox_like(MY_PERSON, activity)
        elif activity.has_type(ap.ActivityType.UNDO):
            obj = activity.get_object()
            if obj.has_type(ap.ActivityType.LIKE):
                back.outbox_undo_like(MY_PERSON, obj)
            elif obj.has_type(ap.ActivityType.ANNOUNCE):
                back.outbox_undo_announce(MY_PERSON, obj)
            elif obj.has_type(ap.ActivityType.FOLLOW):
                back.undo_new_following(MY_PERSON, obj)

        recipients = activity.recipients()
        log.info(f"recipients={recipients}")
        activity = ap.clean_activity(activity.to_dict())

        payload = json.dumps(activity)
        for recp in recipients:
            log.debug(f"posting to {recp}")
            post_to_remote_inbox.delay(payload, recp)
    except Exception as err:
        log.exception(f"failed to cache attachments for {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


@app.task(bind=True, max_retries=12)
def post_to_remote_inbox(self, payload: str, to: str) -> None:
    try:
        log.info("payload=%s", payload)
        log.info("generating sig")
        signed_payload = json.loads(payload)

        # Don't overwrite the signature if we're forwarding an activity
        if "signature" not in signed_payload:
            generate_signature(signed_payload, KEY)

        log.info("to=%s", to)
        resp = requests.post(
            to,
            data=json.dumps(signed_payload),
            auth=SigAuth,
            headers={
                "Content-Type": HEADERS[1],
                "Accept": HEADERS[1],
                "User-Agent": USER_AGENT,
            },
        )
        log.info("resp=%s", resp)
        log.info("resp_body=%s", resp.text)
        resp.raise_for_status()
    except HTTPError as err:
        log.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            log.info("client error, no retry")
            return
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))
