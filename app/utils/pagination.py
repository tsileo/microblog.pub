import base64
from datetime import datetime

from dateutil.parser import isoparse


def encode_cursor(val: datetime) -> str:
    return base64.urlsafe_b64encode(val.isoformat().encode()).decode()


def decode_cursor(cursor: str) -> datetime:
    return isoparse(base64.urlsafe_b64decode(cursor).decode())
