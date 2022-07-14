import asyncio
from dataclasses import dataclass

import humanize
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from tabulate import tabulate

from app import models
from app.config import ROOT_DIR
from app.database import AsyncSession
from app.database import async_session
from app.utils.datetime import now

_DATA_DIR = ROOT_DIR / "data"


@dataclass
class DiskUsageStats:
    data_dir_size: int
    upload_dir_size: int


def get_disk_usage_stats() -> DiskUsageStats:
    du_stats = DiskUsageStats(
        data_dir_size=0,
        upload_dir_size=0,
    )
    for f in _DATA_DIR.glob("**/*"):
        if f.is_file():
            stat = f.stat()
            du_stats.data_dir_size += stat.st_size
            if str(f.parent).endswith("/data/uploads"):
                du_stats.upload_dir_size += stat.st_size

    return du_stats


@dataclass
class OutgoingActivityStatsItem:
    total_count: int
    waiting_count: int
    sent_count: int
    errored_count: int


@dataclass
class OutgoingActivityStats:
    total: OutgoingActivityStatsItem
    from_inbox: OutgoingActivityStatsItem
    from_outbox: OutgoingActivityStatsItem


async def get_outgoing_activity_stats(
    db_session: AsyncSession,
) -> OutgoingActivityStats:
    async def _get_stats(f) -> OutgoingActivityStatsItem:
        row = (
            await db_session.execute(
                select(
                    func.count(models.OutgoingActivity.id).label("total_count"),
                    func.sum(
                        case(
                            [
                                (
                                    or_(
                                        models.OutgoingActivity.next_try > now(),
                                        models.OutgoingActivity.tries == 0,
                                    ),
                                    1,
                                ),
                            ],
                            else_=0,
                        )
                    ).label("waiting_count"),
                    func.sum(
                        case(
                            [
                                (models.OutgoingActivity.is_sent.is_(True), 1),
                            ],
                            else_=0,
                        )
                    ).label("sent_count"),
                    func.sum(
                        case(
                            [
                                (models.OutgoingActivity.is_errored.is_(True), 1),
                            ],
                            else_=0,
                        )
                    ).label("errored_count"),
                ).where(f)
            )
        ).one()
        return OutgoingActivityStatsItem(
            total_count=row.total_count or 0,
            waiting_count=row.waiting_count or 0,
            sent_count=row.sent_count or 0,
            errored_count=row.errored_count or 0,
        )

    from_inbox = await _get_stats(models.OutgoingActivity.inbox_object_id.is_not(None))
    from_outbox = await _get_stats(
        models.OutgoingActivity.outbox_object_id.is_not(None)
    )

    return OutgoingActivityStats(
        from_inbox=from_inbox,
        from_outbox=from_outbox,
        total=OutgoingActivityStatsItem(
            total_count=from_inbox.total_count + from_outbox.total_count,
            waiting_count=from_inbox.waiting_count + from_outbox.waiting_count,
            sent_count=from_inbox.sent_count + from_outbox.sent_count,
            errored_count=from_inbox.errored_count + from_outbox.errored_count,
        ),
    )


def print_stats() -> None:
    async def _get_stats():
        async with async_session() as db_session:
            outgoing_activity_stats = await get_outgoing_activity_stats(db_session)

            outgoing_activities = (
                (
                    await db_session.scalars(
                        select(models.OutgoingActivity)
                        .options(
                            joinedload(models.OutgoingActivity.inbox_object),
                            joinedload(models.OutgoingActivity.outbox_object),
                        )
                        .order_by(models.OutgoingActivity.last_try.desc())
                        .limit(10)
                    )
                )
                .unique()
                .all()
            )

        return outgoing_activity_stats, outgoing_activities

    outgoing_activity_stats, outgoing_activities = asyncio.run(_get_stats())
    disk_usage_stats = get_disk_usage_stats()

    print()
    print(
        tabulate(
            [
                (
                    "data/",
                    humanize.naturalsize(disk_usage_stats.data_dir_size),
                ),
                (
                    "data/uploads/",
                    humanize.naturalsize(disk_usage_stats.upload_dir_size),
                ),
            ],
            headers=["Disk usage", "size"],
        )
    )
    print()
    print(
        tabulate(
            [
                (name, s.total_count, s.waiting_count, s.sent_count, s.errored_count)
                for (name, s) in [
                    ("total", outgoing_activity_stats.total),
                    ("outbox", outgoing_activity_stats.from_outbox),
                    ("forwarded", outgoing_activity_stats.from_inbox),
                ]
            ],
            headers=["Outgoing activities", "total", "waiting", "sent", "errored"],
        )
    )
    print()
    print("Outgoing activities log")
    print("=======================")
    print()
    print(
        tabulate(
            [
                (
                    row.anybox_object.ap_id,
                    humanize.naturaltime(row.last_try),
                    row.recipient,
                    row.tries,
                    row.last_status_code,
                    row.is_sent,
                    row.is_errored,
                )
                for row in outgoing_activities
            ],
            headers=[
                "Object",
                "last try",
                "recipient",
                "tries",
                "status code",
                "sent",
                "errored",
            ],
        )
    )
    print()
