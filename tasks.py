import json
import logging
import os
import random

import requests
from celery import Celery
from little_boxes import activitypub as ap
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import ActivityNotFoundError
from little_boxes.httpsig import HTTPSigAuth
from little_boxes.linked_data_sig import generate_signature
from requests.exceptions import HTTPError

import activitypub
from config import DB
from config import HEADERS
from config import ID
from config import KEY
from config import MEDIA_CACHE
from config import USER_AGENT
from utils.media import Kind

log = logging.getLogger(__name__)
app = Celery(
    "tasks", broker=os.getenv("MICROBLOGPUB_AMQP_BROKER", "pyamqp://guest@localhost//")
)
SigAuth = HTTPSigAuth(KEY)


back = activitypub.MicroblogPubBackend()
ap.use_backend(back)


@app.task(bind=True, max_retries=12)  # noqa: C901
def process_new_activity(self, iri: str) -> None:
    """Process an activity received in the inbox"""
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")

        # Is the activity expected?
        # following = ap.get_backend().following()

        tag_stream = False
        if activity.has_type(ap.ActivityType.ANNOUNCE):
            tag_stream = True
        elif activity.has_type(ap.ActivityType.CREATE):
            note = activity.get_object()
            if note.inReplyTo:
                reply = ap.fetch_remote_activity(note.inReplyTo)
                if (
                    reply.id.startswith(ID) or reply.has_mention(ID)
                ) and activity.is_public():
                    # The reply is public "local reply", forward the reply (i.e. the original activity) to the original
                    # recipients
                    activity.forward(back.followers_as_recipients())

            # (partial) Ghost replies handling
            # [X] This is the first time the server has seen this Activity.
            should_forward = False
            local_followers = ID + "/followers"
            for field in ["to", "cc"]:
                if field in activity._data:
                    if local_followers in activity._data[field]:
                        # [X] The values of to, cc, and/or audience contain a Collection owned by the server.
                        should_forward = True
            if not (note.inReplyTo and note.inReplyTo.startswith(ID)):
                # [X] The values of inReplyTo, object, target and/or tag are objects owned by the server
                should_forward = False

            if should_forward:
                activity.forward(back.followers_as_recipients())
            else:
                tag_stream = True

        log.info(f"{iri} tag_stream={tag_stream}")
        DB.activities.update_one(
            {"remote_id": activity.id}, {"$set": {"meta.stream": tag_stream}}
        )

        log.info(f"new activity {iri} processed")
        cache_actor.delay(iri)
    except (ActivityGoneError, ActivityNotFoundError):
        log.exception(f"dropping activity {iri}, skip processing")
    except Exception as err:
        log.exception(f"failed to process new activity {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


@app.task(bind=True, max_retries=12)
def cache_actor(self, iri: str, also_cache_attachments: bool = True) -> None:
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")

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
                MEDIA_CACHE.cache(attachment["url"], Kind.ATTACHMENT)

        log.info(f"attachments cached for {iri}")

    except (ActivityGoneError, ActivityNotFoundError):
        log.exception(f"dropping activity {iri}, no attachment caching")
    except Exception as err:
        log.exception(f"failed to cache attachments for {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


@app.task(bind=True, max_retries=12)
def post_to_inbox(self, payload: str, to: str) -> None:
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
