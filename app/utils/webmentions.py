from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup  # type: ignore
from loguru import logger

from app import config
from app.utils.url import is_url_valid


def _make_abs(url: str | None, parent: str) -> str | None:
    if url is None:
        return None

    if url.startswith("http"):
        return url

    return (
        urlparse(parent)._replace(path=url, params="", query="", fragment="").geturl()
    )


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
        except (httpx.HTTPError, httpx.HTTPStatusError):
            logger.exception(f"Failed to discover webmention endpoint for {url}")
            return None

    for k, v in resp.links.items():
        if k and "webmention" in k:
            return _make_abs(resp.links[k].get("url"), url)

    soup = BeautifulSoup(resp.text, "html5lib")
    wlinks = soup.find_all(["link", "a"], attrs={"rel": "webmention"})
    for wlink in wlinks:
        if "href" in wlink.attrs:
            return _make_abs(wlink.attrs["href"], url)

    return None


async def discover_webmention_endpoint(url: str) -> str | None:
    """Discover the Webmention endpoint of a given URL, if any.

    Passes all the tests at https://webmention.rocks!

    """
    wurl = await _discover_webmention_endoint(url)
    if wurl is None:
        return None
    if not is_url_valid(wurl):
        return None
    return wurl
