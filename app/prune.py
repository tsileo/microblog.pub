from datetime import timedelta

from loguru import logger
from sqlalchemy import and_
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import not_
from sqlalchemy import or_
from sqlalchemy import select

from app import activitypub as ap
from app import models
from app.config import BASE_URL
from app.config import INBOX_RETENTION_DAYS
from app.database import AsyncSession
from app.database import async_session
from app.utils.datetime import now


async def prune_old_data(
    db_session: AsyncSession,
) -> None:
    logger.info(f"Pruning old data with {INBOX_RETENTION_DAYS=}")
    await _prune_old_incoming_activities(db_session)
    await _prune_old_outgoing_activities(db_session)
    await _prune_old_inbox_objects(db_session)

    # TODO: delete actor with no remaining inbox objects

    await db_session.commit()
    # Reclaim disk space
    await db_session.execute("VACUUM")  # type: ignore


async def _prune_old_incoming_activities(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        delete(models.IncomingActivity)
        .where(
            models.IncomingActivity.created_at
            < now() - timedelta(days=INBOX_RETENTION_DAYS),
            # Keep failed activity for debug
            models.IncomingActivity.is_errored.is_(False),
        )
        .execution_options(synchronize_session=False)
    )
    logger.info(f"Deleted {result.rowcount} old incoming activities")  # type: ignore


async def _prune_old_outgoing_activities(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        delete(models.OutgoingActivity)
        .where(
            models.OutgoingActivity.created_at
            < now() - timedelta(days=INBOX_RETENTION_DAYS),
            # Keep failed activity for debug
            models.OutgoingActivity.is_errored.is_(False),
        )
        .execution_options(synchronize_session=False)
    )
    logger.info(f"Deleted {result.rowcount} old outgoing activities")  # type: ignore


async def _prune_old_inbox_objects(
    db_session: AsyncSession,
) -> None:
    outbox_conversation = select(func.distinct(models.OutboxObject.conversation)).where(
        models.OutboxObject.conversation.is_not(None),
        models.OutboxObject.conversation.not_like(f"{BASE_URL}%"),
    )
    result = await db_session.execute(
        delete(models.InboxObject)
        .where(
            # Keep bookmarked objects
            models.InboxObject.is_bookmarked.is_(False),
            # Keep liked objects
            models.InboxObject.liked_via_outbox_object_ap_id.is_(None),
            # Keep announced objects
            models.InboxObject.announced_via_outbox_object_ap_id.is_(None),
            # Keep objects mentioning the local actor
            models.InboxObject.has_local_mention.is_(False),
            # Keep objects related to local conversations (i.e. don't break the
            # public website)
            or_(
                models.InboxObject.conversation.not_like(f"{BASE_URL}%"),
                models.InboxObject.conversation.is_(None),
                models.InboxObject.conversation.not_in(outbox_conversation),
            ),
            # Keep activities related to the outbox (like Like/Announce/Follow...)
            or_(
                # XXX: no `/` here because the local ID does not have one
                models.InboxObject.activity_object_ap_id.not_like(f"{BASE_URL}%"),
                models.InboxObject.activity_object_ap_id.is_(None),
            ),
            # Keep direct messages
            not_(
                and_(
                    models.InboxObject.visibility == ap.VisibilityEnum.DIRECT,
                    models.InboxObject.ap_type.in_(["Note"]),
                )
            ),
            # Keep Move object as they are linked to notifications
            models.InboxObject.ap_type.not_in(["Move"]),
            # Filter by retention days
            models.InboxObject.ap_published_at
            < now() - timedelta(days=INBOX_RETENTION_DAYS),
        )
        .execution_options(synchronize_session=False)
    )
    logger.info(f"Deleted {result.rowcount} old inbox objects")  # type: ignore


async def run_prune_old_data() -> None:
    """CLI entrypoint."""
    async with async_session() as db_session:
        await prune_old_data(db_session)
