import asyncio
from dataclasses import dataclass

import humanize
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from tabulate import tabulate

from app import models
from app.config import ROOT_DIR
from app.database import AsyncSession
from app.database import async_session
from app.database import now

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
        return OutgoingActivityStatsItem(**dict(row))

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
        async with async_session() as session:
            dat = await get_outgoing_activity_stats(session)

        return dat

    outgoing_activity_stats = asyncio.run(_get_stats())
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
