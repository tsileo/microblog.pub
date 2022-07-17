from datetime import datetime
from datetime import timezone

from dateutil.parser import isoparse


def parse_isoformat(isodate: str) -> datetime:
    return isoparse(isodate).astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)
