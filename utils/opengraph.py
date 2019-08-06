import logging
from typing import Any
from typing import Dict
from typing import Set
from urllib.parse import urlparse

import opengraph
import requests
from bs4 import BeautifulSoup
from little_boxes import activitypub as ap
from little_boxes.errors import NotAnActivityError
from little_boxes.urlutils import check_url
from little_boxes.urlutils import is_url_valid

from .lookup import lookup

logger = logging.getLogger(__name__)


def links_from_note(note: Dict[str, Any]) -> Set[str]:
    note_host = urlparse(ap._get_id(note["id"]) or "").netloc

    links = set()
    if "content" in note:
        soup = BeautifulSoup(note["content"], "html5lib")
        for link in soup.find_all("a"):
            h = link.get("href")
            ph = urlparse(h)
            if (
                ph.scheme in {"http", "https"}
                and ph.netloc != note_host
                and is_url_valid(h)
            ):
                links.add(h)

    # FIXME(tsileo): support summary and name fields

    return links


def fetch_og_metadata(user_agent, links):
    res = []
    for l in links:
        check_url(l)

        # Remove any AP objects
        try:
            lookup(l)
            continue
        except NotAnActivityError:
            pass
        except Exception:
            logger.exception(f"skipping {l} because of issues during AP lookup")
            continue

        try:
            h = requests.head(l, headers={"User-Agent": user_agent}, timeout=3)
            h.raise_for_status()
        except requests.HTTPError as http_err:
            logger.debug(f"failed to HEAD {l}, got a {http_err.response.status_code}")
            continue

        if not h.headers.get("content-type").startswith("text/html"):
            logger.debug(f"skipping {l} for bad content type")
            continue

        try:
            r = requests.get(l, headers={"User-Agent": user_agent}, timeout=5)
            r.raise_for_status()
        except requests.HTTPError as http_err:
            logger.debug(f"failed to GET {l}, got a {http_err.response.status_code}")
            continue

        r.encoding = "UTF-8"
        html = r.text
        try:
            data = dict(opengraph.OpenGraph(html=html))
        except Exception:
            logger.exception(f"failed to parse {l}")
            continue

        # Keep track of the fetched URL as some crappy websites use relative URLs everywhere
        data["_input_url"] = l
        u = urlparse(l)

        # If it's a relative URL, build the absolute version
        if "image" in data and data["image"].startswith("/"):
            data["image"] = u._replace(
                path=data["image"], params="", query="", fragment=""
            ).geturl()

        if "url" in data and data["url"].startswith("/"):
            data["url"] = u._replace(
                path=data["url"], params="", query="", fragment=""
            ).geturl()

        if data.get("url"):
            res.append(data)

    return res
