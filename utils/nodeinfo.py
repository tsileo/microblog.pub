from enum import Enum
from enum import unique
from functools import lru_cache
from typing import Optional

import little_boxes.activitypub as ap
import requests


@unique
class SoftwareName(Enum):
    UNKNOWN = "unknown"
    MASTODON = "mastodon"
    MICROBLOGPUB = "microblogpub"


def _get_nodeinfo_url(server: str) -> Optional[str]:
    backend = ap.get_backend()
    for scheme in {"https", "http"}:
        try:
            resp = requests.get(
                f"{scheme}://{server}/.well-known/nodeinfo",
                timeout=10,
                allow_redirects=True,
                headers={"User-Agent": backend.user_agent()},
            )
            resp.raise_for_status()
            data = resp.json()
            for link in data.get("links", []):
                return link["href"]
        except requests.HTTPError:
            return None
        except requests.RequestException:
            continue

    return None


def _try_mastodon_api(server: str) -> bool:
    for scheme in {"https", "http"}:
        try:
            resp = requests.get(f"{scheme}://{server}/api/v1/instance")
            resp.raise_for_status()
            if resp.json():
                return True
        except requests.HTTPError:
            return False
        except requests.RequestException:
            continue

    return False


@lru_cache(2048)
def get_software_name(server: str) -> str:
    backend = ap.get_backend()
    nodeinfo_endpoint = _get_nodeinfo_url(server)
    if nodeinfo_endpoint:
        try:
            resp = requests.get(
                nodeinfo_endpoint,
                timeout=10,
                headers={"User-Agent": backend.user_agent()},
            )
            resp.raise_for_status()
            software_name = resp.json().get("software", {}).get("name")
            if software_name:
                return software_name

            return SoftwareName.UNKNOWN.value
        except requests.RequestException:
            return SoftwareName.UNKNOWN.value

    if _try_mastodon_api(server):
        return SoftwareName.MASTODON.value

    return SoftwareName.UNKNOWN.value
