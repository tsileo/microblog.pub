from urllib.parse import urlparse

from core.db import _Q
from core.db import update_one_remote
from utils import now


def server(url: str) -> str:
    return urlparse(url).netloc


def _update(url: str, replace: _Q) -> None:
    update_one_remote({"server": server(url)}, replace, upsert=True)


def track_successful_send(url: str) -> None:
    now_ = now()
    _update(
        url,
        {
            "$inc": {"successful_send": 1},
            "$set": {"last_successful_contact": now_, "last_contact": now_},
        },
    )
    return None


def track_failed_send(url: str) -> None:
    now_ = now()
    _update(url, {"$inc": {"failed_send": 1}, "$set": {"last_contact": now_}})
    return None
