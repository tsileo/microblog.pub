import asyncio
import email
import time
import traceback
from datetime import datetime
from datetime import timedelta
from typing import MutableMapping

import httpx
from cachetools import TTLCache
from loguru import logger
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app import activitypub as ap
from app import config
from app import ldsig
from app import models
from app.actor import LOCAL_ACTOR
from app.actor import _actor_hash
from app.config import KEY_PATH
from app.database import AsyncSession
from app.key import Key
from app.utils.datetime import now
from app.utils.url import check_url
from app.utils.workers import Worker

_MAX_RETRIES = 16

_LD_SIG_CACHE: MutableMapping[str, ap.RawObject] = TTLCache(maxsize=5, ttl=60 * 5)


k = Key(config.ID, f"{config.ID}#main-key")
k.load(KEY_PATH.read_text())


def _is_local_actor_updated() -> bool:
    """Returns True if the local actor was updated, i.e. updated via the config file"""
    actor_hash = _actor_hash(LOCAL_ACTOR)
    actor_hash_cache = config.ROOT_DIR / "data" / "local_actor_hash.dat"

    if not actor_hash_cache.exists():
        logger.info("Initializing local actor hash cache")
        actor_hash_cache.write_bytes(actor_hash)
        return False

    previous_actor_hash = actor_hash_cache.read_bytes()
    if previous_actor_hash == actor_hash:
        logger.info("Local actor hasn't been updated")
        return False

    actor_hash_cache.write_bytes(actor_hash)
    logger.info("Local actor has been updated")
    return True


async def _send_actor_update_if_needed(
    db_session: AsyncSession,
) -> None:
    """The process for sending an update for the local actor is done here as
    in production, we may have multiple uvicorn worker and this worker will
    always run in a single process."""
    if not _is_local_actor_updated():
        return

    logger.info("Will send an Update for the local actor")

    from app.boxes import allocate_outbox_id
    from app.boxes import compute_all_known_recipients
    from app.boxes import outbox_object_id
    from app.boxes import save_outbox_object

    update_activity_id = allocate_outbox_id()
    update_activity = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(update_activity_id),
        "type": "Update",
        "to": [ap.AS_PUBLIC],
        "actor": config.ID,
        "object": ap.remove_context(LOCAL_ACTOR.ap_actor),
    }
    outbox_object = await save_outbox_object(
        db_session, update_activity_id, update_activity
    )

    # Send the update to the followers collection and all the actor we have ever
    # contacted
    recipients = await compute_all_known_recipients(db_session)
    for rcp in recipients:
        await new_outgoing_activity(
            db_session,
            recipient=rcp,
            outbox_object_id=outbox_object.id,
        )

    await db_session.commit()


async def new_outgoing_activity(
    db_session: AsyncSession,
    recipient: str,
    outbox_object_id: int | None = None,
    inbox_object_id: int | None = None,
    webmention_target: str | None = None,
) -> models.OutgoingActivity:
    if outbox_object_id is None and inbox_object_id is None:
        raise ValueError("Must reference at least one inbox/outbox activity")
    if webmention_target and outbox_object_id is None:
        raise ValueError("Webmentions must reference an outbox activity")
    if outbox_object_id and inbox_object_id:
        raise ValueError("Cannot reference both inbox/outbox activities")

    outgoing_activity = models.OutgoingActivity(
        recipient=recipient,
        outbox_object_id=outbox_object_id,
        inbox_object_id=inbox_object_id,
        webmention_target=webmention_target,
    )

    db_session.add(outgoing_activity)
    await db_session.flush()
    await db_session.refresh(outgoing_activity)
    return outgoing_activity


def _parse_retry_after(retry_after: str) -> datetime | None:
    try:
        # Retry-After: 120
        seconds = int(retry_after)
    except ValueError:
        # Retry-After: Wed, 21 Oct 2015 07:28:00 GMT
        dt_tuple = email.utils.parsedate_tz(retry_after)
        if dt_tuple is None:
            return None

        seconds = int(email.utils.mktime_tz(dt_tuple) - time.time())

    return now() + timedelta(seconds=seconds)


def _exp_backoff(tries: int) -> datetime:
    seconds = 2 * (2 ** (tries - 1))
    return now() + timedelta(seconds=seconds)


def _set_next_try(
    outgoing_activity: models.OutgoingActivity,
    next_try: datetime | None = None,
) -> None:
    if not outgoing_activity.tries:
        raise ValueError("Should never happen")

    if outgoing_activity.tries >= _MAX_RETRIES:
        outgoing_activity.is_errored = True
        outgoing_activity.next_try = None
    else:
        outgoing_activity.next_try = next_try or _exp_backoff(outgoing_activity.tries)


async def fetch_next_outgoing_activity(
    db_session: AsyncSession,
) -> models.OutgoingActivity | None:
    where = [
        models.OutgoingActivity.next_try <= now(),
        models.OutgoingActivity.is_errored.is_(False),
        models.OutgoingActivity.is_sent.is_(False),
    ]
    q_count = await db_session.scalar(
        select(func.count(models.OutgoingActivity.id)).where(*where)
    )
    if q_count > 0:
        logger.info(f"{q_count} outgoing activities ready to process")
    if not q_count:
        # logger.debug("No activities to process")
        return None

    next_activity = (
        await db_session.execute(
            select(models.OutgoingActivity)
            .where(*where)
            .limit(1)
            .options(
                joinedload(models.OutgoingActivity.inbox_object),
                joinedload(models.OutgoingActivity.outbox_object),
            )
            .order_by(models.OutgoingActivity.next_try)
        )
    ).scalar_one()
    return next_activity


async def process_next_outgoing_activity(
    db_session: AsyncSession,
    next_activity: models.OutgoingActivity,
) -> None:
    next_activity.tries = next_activity.tries + 1  # type: ignore
    next_activity.last_try = now()

    logger.info(f"recipient={next_activity.recipient}")

    try:
        if next_activity.webmention_target and next_activity.outbox_object:
            webmention_payload = {
                "source": next_activity.outbox_object.url,
                "target": next_activity.webmention_target,
            }
            logger.info(f"{webmention_payload=}")
            check_url(next_activity.recipient)
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    next_activity.recipient,  # type: ignore
                    data=webmention_payload,
                    headers={
                        "User-Agent": config.USER_AGENT,
                    },
                )
            resp.raise_for_status()
        else:
            payload = ap.wrap_object_if_needed(next_activity.anybox_object.ap_object)

            # Use LD sig if the activity may need to be forwarded by recipients
            if next_activity.anybox_object.is_from_outbox and payload["type"] in [
                "Create",
                "Update",
                "Delete",
            ]:
                # But only if the object is public (to help with deniability/privacy)
                if next_activity.outbox_object.visibility == ap.VisibilityEnum.PUBLIC:  # type: ignore  # noqa: E501
                    if p := _LD_SIG_CACHE.get(payload["id"]):
                        payload = p
                    else:
                        ldsig.generate_signature(payload, k)
                        _LD_SIG_CACHE[payload["id"]] = payload

            logger.info(f"{payload=}")

            resp = await ap.post(next_activity.recipient, payload)  # type: ignore
    except httpx.HTTPStatusError as http_error:
        logger.exception("Failed")
        next_activity.last_status_code = http_error.response.status_code
        next_activity.last_response = http_error.response.text
        next_activity.error = traceback.format_exc()

        if http_error.response.status_code in [429, 503]:
            retry_after: datetime | None = None
            if retry_after_value := http_error.response.headers.get("Retry-After"):
                retry_after = _parse_retry_after(retry_after_value)
            _set_next_try(next_activity, retry_after)
        elif http_error.response.status_code == 401:
            _set_next_try(next_activity)
        elif 400 <= http_error.response.status_code < 500:
            logger.info(f"status_code={http_error.response.status_code} not retrying")
            next_activity.is_errored = True
            next_activity.next_try = None
        else:
            _set_next_try(next_activity)
    except Exception:
        logger.exception("Failed")
        next_activity.error = traceback.format_exc()
        _set_next_try(next_activity)
    else:
        logger.info("Success")
        next_activity.is_sent = True
        next_activity.last_status_code = resp.status_code
        next_activity.last_response = resp.text

    await db_session.commit()
    return None


class OutgoingActivityWorker(Worker[models.OutgoingActivity]):
    async def process_message(
        self,
        db_session: AsyncSession,
        next_activity: models.OutgoingActivity,
    ) -> None:
        await process_next_outgoing_activity(db_session, next_activity)

    async def get_next_message(
        self,
        db_session: AsyncSession,
    ) -> models.OutgoingActivity | None:
        return await fetch_next_outgoing_activity(db_session)

    async def startup(self, db_session: AsyncSession) -> None:
        await _send_actor_update_if_needed(db_session)


async def loop() -> None:
    await OutgoingActivityWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(loop())
