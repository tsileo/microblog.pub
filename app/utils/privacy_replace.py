from urllib.parse import urlparse

from bs4 import BeautifulSoup  # type: ignore
from loguru import logger

from app.config import PRIVACY_REPLACE


def replace_content(content: str) -> str:
    if not PRIVACY_REPLACE:
        return content

    soup = BeautifulSoup(content, "html5lib")
    links = list(soup.find_all("a", href=True))
    if not links:
        return content

    for link in links:
        link.attrs["href"] = replace_url(link.attrs["href"])

    return soup.find("body").decode_contents()


def replace_url(u: str) -> str:
    if not PRIVACY_REPLACE:
        return u

    try:
        parsed_href = urlparse(u)
    except Exception:
        logger.warning(f"Failed to parse url={u}")
        return u

    if new_netloc := PRIVACY_REPLACE.get(parsed_href.netloc.removeprefix("www.")):
        return parsed_href._replace(netloc=new_netloc).geturl()

    return u
