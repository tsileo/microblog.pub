from datetime import datetime
from datetime import timezone

from dateutil.parser import isoparse


def parse_isoformat(isodate: str) -> datetime:
    return isoparse(isodate).astimezone(timezone.utc)
