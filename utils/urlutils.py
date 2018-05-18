import logging
import socket
import ipaddress
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def is_url_valid(url):
    parsed = urlparse(url)
    if parsed.scheme not in ['http', 'https']:
        return False

    if parsed.hostname in ['localhost']:
        return False

    try:
        ip_address = socket.getaddrinfo(parsed.hostname, parsed.port or 80)[0][4][0]
    except socket.gaierror:
        logger.exception(f'failed to lookup url {url}')
        return False

    if ipaddress.ip_address(ip_address).is_private:
        logger.info(f'rejecting private URL {url}')
        return False

    return True
