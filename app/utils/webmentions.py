from dataclasses import dataclass
from typing import Any
from typing import Optional

import httpx
from bs4 import BeautifulSoup  # type: ignore
from loguru import logger

from app import config
from app.utils.datetime import now
from app.utils.url import check_url
from app.utils.url import is_url_valid
from app.utils.url import make_abs


async def _discover_webmention_endoint(url: str) -> str | None:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": config.USER_AGENT,
                },
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception:
            logger.exception(f"Failed to discover webmention endpoint for {url}")
            return None

    for k, v in resp.links.items():
        if k and "webmention" in k:
            return make_abs(resp.links[k].get("url"), url)

    soup = BeautifulSoup(resp.text, "html5lib")
    wlinks = soup.find_all(["link", "a"], attrs={"rel": "webmention"})
    for wlink in wlinks:
        if "href" in wlink.attrs:
            return make_abs(wlink.attrs["href"], url)

    return None


async def discover_webmention_endpoint(url: str) -> str | None:
    """Discover the Webmention endpoint of a given URL, if any.

    Passes all the tests at https://webmention.rocks!

    """
    check_url(url)

    wurl = await _discover_webmention_endoint(url)
    if wurl is None:
        return None
    if not is_url_valid(wurl):
        return None
    return wurl


@dataclass
class Webmention:
    actor_icon_url: str
    actor_name: str
    url: str
    received_at: str

    @classmethod
    def from_microformats(
        cls, items: list[dict[str, Any]], url: str
    ) -> Optional["Webmention"]:
        for item in items:
            if item["type"][0] == "h-card":
                return cls(
                    actor_icon_url=make_abs(
                        item["properties"]["photo"][0], url
                    ),  # type: ignore
                    actor_name=item["properties"]["name"][0],
                    url=url,
                    received_at=now().isoformat(),
                )
            if item["type"][0] == "h-entry":
                author = item["properties"]["author"][0]
                return cls(
                    actor_icon_url=make_abs(
                        author["properties"]["photo"][0], url
                    ),  # type: ignore
                    actor_name=author["properties"]["name"][0],
                    url=url,
                    received_at=now().isoformat(),
                )

        return None
