import logging
from typing import Any
from typing import Dict
from typing import Optional
from urllib.parse import urlparse

import requests

from .urlutils import check_url

logger = logging.getLogger(__name__)


def webfinger(resource: str) -> Optional[Dict[str, Any]]:
    """Mastodon-like WebFinger resolution to retrieve the activity stream Actor URL.
    """
    logger.info(f'performing webfinger resolution for {resource}')
    protos = ['https', 'http']
    if resource.startswith('http://'):
        protos.reverse()
        host = urlparse(resource).netloc
    elif resource.startswith('https://'):
        host = urlparse(resource).netloc
    else:
        if resource.startswith('acct:'):
            resource = resource[5:]
        if resource.startswith('@'):
            resource = resource[1:]
        _, host = resource.split('@', 1)
        resource='acct:'+resource

    # Security check on the url (like not calling localhost)
    check_url(f'https://{host}')

    for i, proto in enumerate(protos):
        try:
            url = f'{proto}://{host}/.well-known/webfinger'
            resp = requests.get(
                url,
                {'resource': resource}
            )
        except requests.ConnectionError:
            # If we tried https first and the domain is "http only"
            if i == 0:
                continue
            break
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()
 

def get_remote_follow_template(resource: str) -> Optional[str]:
    data = webfinger(resource)
    if data is None:
        return None
    for link in data['links']:
        if link.get('rel') == 'http://ostatus.org/schema/1.0/subscribe':
            return link.get('template')
    return None


def get_actor_url(resource: str) -> Optional[str]:
    """Mastodon-like WebFinger resolution to retrieve the activity stream Actor URL.

    Returns:
        the Actor URL or None if the resolution failed.
    """
    data = webfinger(resource)
    if data is None:
        return None
    for link in data['links']:
        if link.get('rel') == 'self' and link.get('type') == 'application/activity+json':
            return link.get('href')
    return None
