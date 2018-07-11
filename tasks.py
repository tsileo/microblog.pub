import json
import logging
import os
import random

import requests
from celery import Celery
from requests.exceptions import HTTPError

from little_boxes import activitypub as ap
from config import DB
from config import HEADERS
from config import KEY
from config import USER_AGENT
from little_boxes.httpsig import HTTPSigAuth
from little_boxes.linked_data_sig import generate_signature
from utils.opengraph import fetch_og_metadata
from utils.media import Kind
from config import MEDIA_CACHE

log = logging.getLogger(__name__)
app = Celery(
    "tasks", broker=os.getenv("MICROBLOGPUB_AMQP_BROKER", "pyamqp://guest@localhost//")
)
SigAuth = HTTPSigAuth(KEY)


@app.task(bind=True, max_retries=12)
def process_new_activity(self, iri: str) -> None:
    try:
        activity = ap.fetch_remote_activity(iri)
        log.info(f"activity={activity!r}")

        tag_stream = False
        if activity.has_type(ap.ActivityType.ANNOUCE):
            tag_stream = True
        elif activity.has_type(ap.ActivityType.CREATE):
            note = activity.get_object()
            if not note.inReplyTo:
                tag_stream = True

        log.info(f"{iri} tag_stream={tag_stream}")
        DB.update_one({"remote_id": activity.id}, {"$set": {"meta.stream": tag_stream}})

        log.info(f"new activity {iri} processed")
    except Exception as err:
        log.exception(f"failed to process new activity {iri}")
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

        log.info(f"attachmwents cached for {iri}")

    except Exception as err:
        log.exception(f"failed to process new activity {iri}")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


@app.task(bind=True, max_retries=12)
def post_to_inbox(self, payload: str, to: str) -> None:
    try:
        log.info("payload=%s", payload)
        log.info("generating sig")
        signed_payload = json.loads(payload)
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


@app.task(bind=True, max_retries=12)
def fetch_og(self, col, remote_id):
    try:
        log.info("fetch_og_meta remote_id=%s col=%s", remote_id, col)
        if col == "INBOX":
            log.info(
                "%d links saved", fetch_og_metadata(USER_AGENT, DB.inbox, remote_id)
            )
        elif col == "OUTBOX":
            log.info(
                "%d links saved", fetch_og_metadata(USER_AGENT, DB.outbox, remote_id)
            )
    except Exception as err:
        self.log.exception("failed")
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))
