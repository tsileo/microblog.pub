import email
import time
import traceback
from datetime import datetime
from datetime import timedelta

import httpx
from loguru import logger
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app import activitypub as ap
from app import config
from app import ldsig
from app import models
from app.actor import LOCAL_ACTOR
from app.actor import _actor_hash
from app.config import KEY_PATH
from app.database import AsyncSession
from app.database import SessionLocal
from app.database import now
from app.key import Key

_MAX_RETRIES = 16

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


def _send_actor_update_if_needed(db_session: Session) -> None:
    """The process for sending an update for the local actor is done here as
    in production, we may have multiple uvicorn worker and this worker will
    always run in a single process."""
    if not _is_local_actor_updated():
        return

    logger.info("Will send an Update for the local actor")

    from app.boxes import RemoteObject
    from app.boxes import allocate_outbox_id
    from app.boxes import outbox_object_id

    update_activity_id = allocate_outbox_id()
    update_activity = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(update_activity_id),
        "type": "Update",
        "to": [ap.AS_PUBLIC],
        "actor": config.ID,
        "object": ap.remove_context(LOCAL_ACTOR.ap_actor),
    }
    ro = RemoteObject(update_activity, actor=LOCAL_ACTOR)
    outbox_object = models.OutboxObject(
        public_id=update_activity_id,
        ap_type=ro.ap_type,
        ap_id=ro.ap_id,
        ap_context=ro.ap_context,
        ap_object=ro.ap_object,
        visibility=ro.visibility,
        og_meta=None,
        relates_to_inbox_object_id=None,
        relates_to_outbox_object_id=None,
        relates_to_actor_id=None,
        activity_object_ap_id=LOCAL_ACTOR.ap_id,
        is_hidden_from_homepage=True,
        source=None,
    )
    db_session.add(outbox_object)
    db_session.flush()

    # TODO(ts): also send to every actor we contact (distinct on recipient)
    followers = (
        (
            db_session.scalars(
                select(models.Follower).options(joinedload(models.Follower.actor))
            )
        )
        .unique()
        .all()
    )
    for rcp in {
        follower.actor.shared_inbox_url or follower.actor.inbox_url
        for follower in followers
    }:
        outgoing_activity = models.OutgoingActivity(
            recipient=rcp,
            outbox_object_id=outbox_object.id,
            inbox_object_id=None,
        )

        db_session.add(outgoing_activity)

    db_session.commit()


async def new_outgoing_activity(
    db_session: AsyncSession,
    recipient: str,
    outbox_object_id: int | None,
    inbox_object_id: int | None = None,
) -> models.OutgoingActivity:
    if outbox_object_id is None and inbox_object_id is None:
        raise ValueError("Must reference at least one inbox/outbox activity")
    elif outbox_object_id and inbox_object_id:
        raise ValueError("Cannot reference both inbox/outbox activities")

    outgoing_activity = models.OutgoingActivity(
        recipient=recipient,
        outbox_object_id=outbox_object_id,
        inbox_object_id=inbox_object_id,
    )

    db_session.add(outgoing_activity)
    await db_session.commit()
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

    if outgoing_activity.tries == _MAX_RETRIES:
        outgoing_activity.is_errored = True
        outgoing_activity.next_try = None
    else:
        outgoing_activity.next_try = next_try or _exp_backoff(outgoing_activity.tries)


def process_next_outgoing_activity(db: Session) -> bool:
    where = [
        models.OutgoingActivity.next_try <= now(),
        models.OutgoingActivity.is_errored.is_(False),
        models.OutgoingActivity.is_sent.is_(False),
    ]
    q_count = db.scalar(select(func.count(models.OutgoingActivity.id)).where(*where))
    if q_count > 0:
        logger.info(f"{q_count} outgoing activities ready to process")
    if not q_count:
        # logger.debug("No activities to process")
        return False

    next_activity = db.execute(
        select(models.OutgoingActivity)
        .where(*where)
        .limit(1)
        .options(
            joinedload(models.OutgoingActivity.inbox_object),
            joinedload(models.OutgoingActivity.outbox_object),
        )
        .order_by(models.OutgoingActivity.next_try)
    ).scalar_one()

    next_activity.tries = next_activity.tries + 1
    next_activity.last_try = now()

    payload = ap.wrap_object_if_needed(next_activity.anybox_object.ap_object)

    # Use LD sig if the activity may need to be forwarded by recipients
    if next_activity.anybox_object.is_from_outbox and payload["type"] in [
        "Create",
        "Update",
        "Delete",
    ]:
        # But only if the object is public (to help with deniability/privacy)
        if next_activity.outbox_object.visibility == ap.VisibilityEnum.PUBLIC:
            ldsig.generate_signature(payload, k)

    logger.info(f"{payload=}")
    try:
        resp = ap.post(next_activity.recipient, payload)
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

    db.commit()
    return True


def loop() -> None:
    db = SessionLocal()
    _send_actor_update_if_needed(db)
    while 1:
        try:
            process_next_outgoing_activity(db)
        except Exception:
            logger.exception("Failed to process next outgoing activity")
            raise

        time.sleep(1)


if __name__ == "__main__":
    loop()
