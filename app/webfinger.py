import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from app import config
from app.utils.url import check_url


async def get_webfinger_via_host_meta(host: str) -> str | None:
    resp: httpx.Response | None = None
    is_404 = False
    async with httpx.AsyncClient() as client:
        for i, proto in enumerate({"http", "https"}):
            try:
                url = f"{proto}://{host}/.well-known/host-meta"
                check_url(url)
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": config.USER_AGENT,
                    },
                    follow_redirects=True,
                )
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as http_error:
                logger.exception("HTTP error")
                if http_error.response.status_code in [403, 404, 410]:
                    is_404 = True
                    continue
                raise
            except httpx.HTTPError:
                logger.exception("req failed")
                # If we tried https first and the domain is "http only"
                if i == 0:
                    continue
                break

        if is_404:
            return None

    if resp:
        tree = ET.fromstring(resp.text)
        maybe_link = tree.find(
            "./{http://docs.oasis-open.org/ns/xri/xrd-1.0}Link[@rel='lrdd']"
        )
        if maybe_link is not None:
            return maybe_link.attrib.get("template")

    return None


async def webfinger(
    resource: str,
    webfinger_url: str | None = None,
) -> dict[str, Any] | None:  # noqa: C901
    """Mastodon-like WebFinger resolution to retrieve the activity stream Actor URL."""
    resource = resource.strip()
    logger.info(f"performing webfinger resolution for {resource}")
    urls = []
    host = None
    if webfinger_url:
        urls = [webfinger_url]
    else:
        if resource.startswith("http://"):
            host = urlparse(resource).netloc
            url = f"http://{host}/.well-known/webfinger"
        elif resource.startswith("https://"):
            host = urlparse(resource).netloc
            url = f"https://{host}/.well-known/webfinger"
        else:
            protos = ["https", "http"]
            _, host = resource.split("@", 1)
            urls = [f"{proto}://{host}/.well-known/webfinger" for proto in protos]

    if resource.startswith("acct:"):
        resource = resource[5:]
    if resource.startswith("@"):
        resource = resource[1:]
    resource = "acct:" + resource

    is_404 = False

    resp: httpx.Response | None = None
    async with httpx.AsyncClient() as client:
        for i, url in enumerate(urls):
            try:
                check_url(url)
                resp = await client.get(
                    url,
                    params={"resource": resource},
                    headers={
                        "User-Agent": config.USER_AGENT,
                    },
                    follow_redirects=True,
                )
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as http_error:
                logger.exception("HTTP error")
                if http_error.response.status_code in [403, 404, 410]:
                    is_404 = True
                    continue
                raise
            except httpx.HTTPError:
                logger.exception("req failed")
                # If we tried https first and the domain is "http only"
                if i == 0:
                    continue
                break

    if is_404:
        if not webfinger_url and host:
            if webfinger_url := (await get_webfinger_via_host_meta(host)):
                return await webfinger(
                    resource,
                    webfinger_url=webfinger_url,
                )
        return None

    if resp:
        return resp.json()
    else:
        return None


async def get_remote_follow_template(resource: str) -> str | None:
    data = await webfinger(resource)
    if data is None:
        return None
    for link in data["links"]:
        if link.get("rel") == "http://ostatus.org/schema/1.0/subscribe":
            return link.get("template")
    return None


async def get_actor_url(resource: str) -> str | None:
    """Mastodon-like WebFinger resolution to retrieve the activity stream Actor URL.

    Returns:
        the Actor URL or None if the resolution failed.
    """
    data = await webfinger(resource)
    if data is None:
        return None
    for link in data["links"]:
        if (
            link.get("rel") == "self"
            and link.get("type") == "application/activity+json"
        ):
            return link.get("href")
    return None
