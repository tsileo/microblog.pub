import functools
import ipaddress
import socket
from urllib.parse import urlparse

from loguru import logger

from app.config import BLOCKED_SERVERS
from app.config import DEBUG


def make_abs(url: str | None, parent: str) -> str | None:
    if url is None:
        return None

    if url.startswith("http"):
        return url

    return (
        urlparse(parent)._replace(path=url, params="", query="", fragment="").geturl()
    )


class InvalidURLError(Exception):
    pass


@functools.lru_cache(maxsize=256)
def _getaddrinfo(hostname: str, port: int) -> str:
    try:
        ip_address = str(ipaddress.ip_address(hostname))
    except ValueError:
        try:
            ip_address = socket.getaddrinfo(hostname, port)[0][4][0]
            logger.debug(f"DNS lookup: {hostname} -> {ip_address}")
        except socket.gaierror:
            logger.exception(f"failed to lookup addr info for {hostname}")
            raise

    return ip_address


def is_url_valid(url: str) -> bool:
    """Implements basic SSRF protection."""
    parsed = urlparse(url)
    if parsed.scheme not in ["http", "https"]:
        return False

    # XXX in debug mode, we want to allow requests to localhost to test the
    # federation with local instances
    if DEBUG:  # pragma: no cover
        return True

    if not parsed.hostname or parsed.hostname.lower() in ["localhost"]:
        return False

    if is_hostname_blocked(parsed.hostname):
        logger.warning(f"{parsed.hostname} is blocked")
        return False

    if parsed.hostname.endswith(".onion"):
        logger.warning(f"{url} is an onion service")
        return False

    ip_address = _getaddrinfo(
        parsed.hostname, parsed.port or (80 if parsed.scheme == "http" else 443)
    )
    logger.debug(f"{ip_address=}")

    if ipaddress.ip_address(ip_address).is_private:
        logger.info(f"rejecting private URL {url} -> {ip_address}")
        return False

    return True


@functools.lru_cache(maxsize=512)
def check_url(url: str) -> None:
    logger.debug(f"check_url {url=}")
    if not is_url_valid(url):
        raise InvalidURLError(f'"{url}" is invalid')

    return None


@functools.lru_cache(maxsize=256)
def is_hostname_blocked(hostname: str) -> bool:
    for blocked_hostname in BLOCKED_SERVERS:
        if hostname == blocked_hostname or hostname.endswith(f".{blocked_hostname}"):
            return True
    return False
