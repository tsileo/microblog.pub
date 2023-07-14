import asyncio
import traceback
from datetime import datetime
from datetime import timedelta

from loguru import logger
from sqlalchemy import func
from sqlalchemy import select

from app import activitypub as ap
from app import httpsig
from app import ldsig
from app import models
from app.boxes import save_to_inbox
from app.database import AsyncSession
from app.utils.datetime import now
from app.utils.workers import Worker

_MAX_RETRIES = 8


async def new_ap_incoming_activity(
    db_session: AsyncSession,
    httpsig_info: httpsig.HTTPSigInfo,
    raw_object: ap.RawObject,
) -> models.IncomingActivity | None:
    ap_id: str
    if "id" not in raw_object or ap.as_list(raw_object["type"])[0] in ap.ACTOR_TYPES:
        if "@context" not in raw_object:
            logger.warning(f"Dropping invalid object: {raw_object}")
            return None
        else:
            # This is a transient object, Build the JSON LD hash as the ID
            ap_id = ldsig._doc_hash(raw_object)
    else:
        ap_id = ap.get_id(raw_object)

    # TODO(ts): dedup first

    incoming_activity = models.IncomingActivity(
        sent_by_ap_actor_id=httpsig_info.signed_by_ap_actor_id,
        ap_id=ap_id,
        ap_object=raw_object,
    )
    db_session.add(incoming_activity)
    await db_session.commit()
    await db_session.refresh(incoming_activity)
    return incoming_activity


def _exp_backoff(tries: int) -> datetime:
    seconds = 2 * (2 ** (tries - 1))
    return now() + timedelta(seconds=seconds)


def _set_next_try(
    outgoing_activity: models.IncomingActivity,
    next_try: datetime | None = None,
) -> None:
    if not outgoing_activity.tries:
        raise ValueError("Should never happen")

    if outgoing_activity.tries >= _MAX_RETRIES:
        outgoing_activity.is_errored = True
        outgoing_activity.next_try = None
    else:
        outgoing_activity.next_try = next_try or _exp_backoff(outgoing_activity.tries)


async def fetch_next_incoming_activity(
    db_session: AsyncSession,
) -> models.IncomingActivity | None:
    where = [
        models.IncomingActivity.next_try <= now(),
        models.IncomingActivity.is_errored.is_(False),
        models.IncomingActivity.is_processed.is_(False),
    ]
    q_count = await db_session.scalar(
        select(func.count(models.IncomingActivity.id)).where(*where)
    )
    if q_count > 0:
        logger.info(f"{q_count} incoming activities ready to process")
    if not q_count:
        # logger.debug("No activities to process")
        return None

    next_activity = (
        await db_session.execute(
            select(models.IncomingActivity)
            .where(*where)
            .limit(1)
            .order_by(models.IncomingActivity.next_try.asc())
        )
    ).scalar_one()

    return next_activity


async def process_next_incoming_activity(
    db_session: AsyncSession,
    next_activity: models.IncomingActivity,
) -> None:
    logger.info(
        f"incoming_activity={next_activity.ap_object}/"
        f"{next_activity.sent_by_ap_actor_id}"
    )

    next_activity.tries = next_activity.tries + 1
    next_activity.last_try = now()
    await db_session.commit()

    if next_activity.ap_object and next_activity.sent_by_ap_actor_id:
        try:
            async with db_session.begin_nested():
                await asyncio.wait_for(
                    save_to_inbox(
                        db_session,
                        next_activity.ap_object,
                        next_activity.sent_by_ap_actor_id,
                    ),
                    timeout=60,
                )
        except asyncio.exceptions.TimeoutError:
            logger.error("Activity took too long to process")
            await db_session.rollback()
            await db_session.refresh(next_activity)
            next_activity.error = traceback.format_exc()
            _set_next_try(next_activity)
        except Exception:
            logger.exception("Failed")
            await db_session.rollback()
            await db_session.refresh(next_activity)
            next_activity.error = traceback.format_exc()
            _set_next_try(next_activity)
        else:
            logger.info("Success")
            next_activity.is_processed = True

    # FIXME: webmention support

    await db_session.commit()
    return None


class IncomingActivityWorker(Worker[models.IncomingActivity]):
    async def process_message(
        self,
        db_session: AsyncSession,
        next_activity: models.IncomingActivity,
    ) -> None:
        await process_next_incoming_activity(db_session, next_activity)

    async def get_next_message(
        self,
        db_session: AsyncSession,
    ) -> models.IncomingActivity | None:
        return await fetch_next_incoming_activity(db_session)


async def loop() -> None:
    await IncomingActivityWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(loop())
