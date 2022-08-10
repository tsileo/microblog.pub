import mimetypes
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup  # type: ignore
from pydantic import BaseModel

from loguru import logger
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


def _scrap_og_meta(url: str, html: str) -> OpenGraphMeta | None:
    soup = BeautifulSoup(html, "html5lib")
    ogs = {
        og.attrs["property"]: og.attrs.get("content")
        for og in soup.html.head.findAll(property=re.compile(r"^og"))
    }
    raw = {
        "url": url,
        "title": soup.find("title").text,
        "image": None,
        "description": None,
        "site_name": urlparse(url).netloc,
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

    return OpenGraphMeta.parse_obj(raw)


async def external_urls(
    db_session: AsyncSession,
    ro: ap_object.RemoteObject | OutboxObject | InboxObject,
) -> set[str]:
    note_host = urlparse(ro.ap_id).netloc

    tags_hrefs = set()
    for tag in ro.tags:
        if tag_href := tag.get("href"):
            tags_hrefs.add(tag_href)
        if tag.get("type") == "Mention":
            mentioned_actor = await fetch_actor(db_session, tag["href"])
            tags_hrefs.add(mentioned_actor.url)
            tags_hrefs.add(mentioned_actor.ap_id)

    urls = set()
    if ro.content:
        soup = BeautifulSoup(ro.content, "html5lib")
        for link in soup.find_all("a"):
            h = link.get("href")
            ph = urlparse(h)
            mimetype, _ = mimetypes.guess_type(h)
            if (
                ph.scheme in {"http", "https"}
                and ph.netloc != note_host
                and is_url_valid(h)
                and (
                    not mimetype
                    or mimetype.split("/")[0] not in ["image", "video", "audio"]
                )
            ):
                urls.add(h)

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

    return _scrap_og_meta(url, resp.text)


async def og_meta_from_note(
    db_session: AsyncSession,
    ro: ap_object.RemoteObject,
) -> list[dict[str, Any]]:
    og_meta = []
    urls = await external_urls(db_session, ro)
    for url in urls:
        try:
            maybe_og_meta = await _og_meta_from_url(url)
            if maybe_og_meta:
                og_meta.append(maybe_og_meta.dict())
        except httpx.HTTPError:
            pass

    return og_meta
