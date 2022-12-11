import asyncio
import mimetypes
import re
import signal
from concurrent.futures import TimeoutError
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup  # type: ignore
from loguru import logger
from pebble import concurrent  # type: ignore
from pydantic import BaseModel

from app import activitypub as ap
from app import ap_object
from app import config
from app.actor import LOCAL_ACTOR
from app.actor import fetch_actor
from app.database import AsyncSession
from app.models import InboxObject
from app.models import OutboxObject
from app.utils.url import is_url_valid
from app.utils.url import make_abs


class OpenGraphMeta(BaseModel):
    url: str
    title: str
    image: str | None
    description: str | None
    site_name: str


@concurrent.process(timeout=5)
def _scrap_og_meta(url: str, html: str) -> OpenGraphMeta | None:
    # Prevent SIGTERM to bubble up to the worker
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    soup = BeautifulSoup(html, "html5lib")
    ogs = {
        og.attrs["property"]: og.attrs.get("content")
        for og in soup.html.head.findAll(property=re.compile(r"^og"))
    }
    # FIXME some page have no <title>
    raw = {
        "url": url,
        "title": soup.find("title").text.strip(),
        "image": None,
        "description": None,
        "site_name": urlparse(url).hostname,
    }
    for field in OpenGraphMeta.__fields__.keys():
        og_field = f"og:{field}"
        if ogs.get(og_field):
            raw[field] = ogs.get(og_field, None)

    if "title" not in raw:
        return None

    for maybe_rel in {"url", "image"}:
        if u := raw.get(maybe_rel):
            raw[maybe_rel] = make_abs(u, url)

            if not is_url_valid(raw[maybe_rel]):
                logger.info(f"Invalid url {raw[maybe_rel]}")
                if maybe_rel == "url":
                    raw["url"] = url
                elif maybe_rel == "image":
                    raw["image"] = None

    return OpenGraphMeta.parse_obj(raw)


def scrap_og_meta(url: str, html: str) -> OpenGraphMeta | None:
    return _scrap_og_meta(url, html).result()


async def external_urls(
    db_session: AsyncSession,
    ro: ap_object.RemoteObject | OutboxObject | InboxObject,
) -> set[str]:
    note_host = urlparse(ro.ap_id).hostname

    tags_hrefs = set()
    for tag in ro.tags:
        if tag_href := tag.get("href"):
            tags_hrefs.add(tag_href)
        if tag.get("type") == "Mention":
            if tag["href"] != LOCAL_ACTOR.ap_id:
                try:
                    mentioned_actor = await fetch_actor(db_session, tag["href"])
                except (ap.FetchError, ap.NotAnObjectError):
                    tags_hrefs.add(tag["href"])
                    continue

                tags_hrefs.add(mentioned_actor.url)
                tags_hrefs.add(mentioned_actor.ap_id)
            else:
                tags_hrefs.add(LOCAL_ACTOR.ap_id)
                tags_hrefs.add(LOCAL_ACTOR.url)

    urls = set()
    if ro.content:
        soup = BeautifulSoup(ro.content, "html5lib")
        for link in soup.find_all("a"):
            h = link.get("href")
            if not h:
                continue

            try:
                ph = urlparse(h)
                mimetype, _ = mimetypes.guess_type(h)
                if (
                    ph.scheme in {"http", "https"}
                    and ph.hostname != note_host
                    and is_url_valid(h)
                    and (
                        not mimetype
                        or mimetype.split("/")[0] not in ["image", "video", "audio"]
                    )
                ):
                    urls.add(h)
            except Exception:
                logger.exception(f"Failed to check {h}")
                continue

    return urls - tags_hrefs


async def _og_meta_from_url(url: str) -> OpenGraphMeta | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "User-Agent": config.USER_AGENT,
            },
            follow_redirects=True,
        )

    resp.raise_for_status()

    if not (ct := resp.headers.get("content-type")) or not ct.startswith("text/html"):
        return None

    try:
        return scrap_og_meta(url, resp.text)
    except TimeoutError:
        logger.info(f"Timed out when scraping OG meta for {url}")
        return None
    except Exception:
        logger.info(f"Failed to scrap OG meta for {url}")
        return None


async def og_meta_from_note(
    db_session: AsyncSession,
    ro: ap_object.RemoteObject,
) -> list[dict[str, Any]]:
    og_meta = []
    urls = await external_urls(db_session, ro)
    logger.debug(f"Lookig OG metadata in {urls=}")
    for url in urls:
        logger.debug(f"Processing {url}")
        try:
            maybe_og_meta = None
            try:
                maybe_og_meta = await asyncio.wait_for(
                    _og_meta_from_url(url),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                logger.info(f"Timing out fetching {url}")
            except Exception:
                logger.exception(f"Failed scrap OG meta for {url}")

            if maybe_og_meta:
                og_meta.append(maybe_og_meta.dict())
        except httpx.HTTPError:
            pass

    return og_meta
