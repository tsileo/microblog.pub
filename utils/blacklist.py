import logging
from typing import Any
from typing import Dict
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)


def is_url_blacklisted(url: str) -> bool:
    try:
        return urlparse(url).netloc in config.BLACKLIST
    except Exception:
        logger.exception(f"failed to blacklist for {url}")
        return False


def is_blacklisted(data: Dict[str, Any]) -> bool:
    """Returns True if the activity is coming/or referencing a blacklisted host."""
    if (
        "id" in data
        and is_url_blacklisted(data["id"])
        or (
            "object" in data
            and isinstance(data["object"], dict)
            and "id" in data["object"]
            and is_url_blacklisted(data["object"]["id"])
        )
        or (
            "object" in data
            and isinstance(data["object"], str)
            and is_url_blacklisted(data["object"])
        )
    ):
        return True

    return False
