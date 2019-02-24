import logging
import opengraph
import requests
from bs4 import BeautifulSoup
from little_boxes import activitypub as ap
from little_boxes.errors import NotAnActivityError
from little_boxes.urlutils import check_url
from little_boxes.urlutils import is_url_valid

from .lookup import lookup

logger = logging.getLogger(__name__)


def links_from_note(note):
    tags_href = set()
    for t in note.get("tag", []):
        h = t.get("href")
        if h:
            tags_href.add(h)

    links = set()
    soup = BeautifulSoup(note["content"])
    for link in soup.find_all("a"):
        h = link.get("href")
        if h.startswith("http") and h not in tags_href and is_url_valid(h):
            links.add(h)

    return links


def fetch_og_metadata(user_agent, links):
    res = []
    for l in links:
        check_url(l)

        # Remove any AP actor from the list
        try:
            p = lookup(l)
            if p.has_type(ap.ACTOR_TYPES):
                continue
        except NotAnActivityError:
            pass

        r = requests.get(l, headers={"User-Agent": user_agent}, timeout=15)
        r.raise_for_status()
        if not r.headers.get("content-type").startswith("text/html"):
            logger.debug(f"skipping {l}")
            continue

        r.encoding = 'UTF-8'
        html = r.text
        try:
            data = dict(opengraph.OpenGraph(html=html))
        except Exception:
            logger.exception(f"failed to parse {l}")
            continue
        if data.get("url"):
            res.append(data)

    return res
