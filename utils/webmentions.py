import logging
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from little_boxes.urlutils import is_url_valid

logger = logging.getLogger(__name__)


def _make_abs(url: Optional[str], parent: str) -> Optional[str]:
    if url is None:
        return None

    if url.startswith("http"):
        return url

    return (
        urlparse(parent)._replace(path=url, params="", query="", fragment="").geturl()
    )


def _discover_webmention_endoint(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, timeout=3)
    except Exception:
        return None

    for k, v in resp.links.items():
        if "webmention" in k:
            return _make_abs(resp.links[k].get("url"), url)

    soup = BeautifulSoup(resp.text, "html5lib")
    wlinks = soup.find_all(["link", "a"], attrs={"rel": "webmention"})
    for wlink in wlinks:
        if "href" in wlink.attrs:
            return _make_abs(wlink.attrs["href"], url)

    return None


def discover_webmention_endpoint(url: str) -> Optional[str]:
    """Discover the Webmention endpoint of a given URL, if any.

    Passes all the tests at https://webmention.rocks!

    """
    wurl = _discover_webmention_endoint(url)
    if wurl is None:
        return None
    if not is_url_valid(wurl):
        return None
    return wurl
