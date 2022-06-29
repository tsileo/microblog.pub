import mimetypes
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup  # type: ignore
from pydantic import BaseModel

from app import activitypub as ap
from app import config
from app.utils.url import is_url_valid


class OpenGraphMeta(BaseModel):
    url: str
    title: str
    image: str
    description: str
    site_name: str


def _scrap_og_meta(html: str) -> OpenGraphMeta | None:
    soup = BeautifulSoup(html, "html5lib")
    ogs = {
        og.attrs["property"]: og.attrs.get("content")
        for og in soup.html.head.findAll(property=re.compile(r"^og"))
    }
    raw = {}
    for field in OpenGraphMeta.__fields__.keys():
        og_field = f"og:{field}"
        if not ogs.get(og_field):
            return None

        raw[field] = ogs[og_field]

    return OpenGraphMeta.parse_obj(raw)


def _urls_from_note(note: ap.RawObject) -> set[str]:
    note_host = urlparse(ap.get_id(note["id"]) or "").netloc

    urls = set()
    if "content" in note:
        soup = BeautifulSoup(note["content"], "html5lib")
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
                    or mimetype.split("/")[0] in ["image", "video", "audio"]
                )
            ):
                urls.add(h)

    return urls


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

    return _scrap_og_meta(resp.text)


async def og_meta_from_note(note: ap.RawObject) -> list[dict[str, Any]]:
    og_meta = []
    urls = _urls_from_note(note)
    for url in urls:
        try:
            maybe_og_meta = await _og_meta_from_url(url)
            if maybe_og_meta:
                og_meta.append(maybe_og_meta.dict())
        except httpx.HTTPError:
            pass

    return og_meta
