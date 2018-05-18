from typing import Optional
from urllib.parse import urlparse

import requests

def get_remote_follow_template(resource: str) -> Optional[str]:
    """Mastodon-like WebFinger resolution to retrieve the activity stream Actor URL.

    Returns:
        the Actor URL or None if the resolution failed.
    """
    if resource.startswith('http'):
        host = urlparse(resource).netloc
    else:
        if resource.startswith('acct:'):
            resource = resource[5:]
        if resource.startswith('@'):
            resource = resource[1:]
        _, host = resource.split('@', 1)
        resource='acct:'+resource
    resp = requests.get(
        f'https://{host}/.well-known/webfinger',
        {'resource': resource}
    )
    print(resp, resp.request.url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    for link in data['links']:
        if link.get('rel') == 'http://ostatus.org/schema/1.0/subscribe':
            return link.get('template')
    return None


def get_actor_url(resource: str) -> Optional[str]:
    """Mastodon-like WebFinger resolution to retrieve the activity stream Actor URL.

    Returns:
        the Actor URL or None if the resolution failed.
    """
    if resource.startswith('http'):
        host = urlparse(resource).netloc
    else:
        if resource.startswith('acct:'):
            resource = resource[5:]
        if resource.startswith('@'):
            resource = resource[1:]
        _, host = resource.split('@', 1)
        resource='acct:'+resource
    resp = requests.get(
        f'https://{host}/.well-known/webfinger',
        {'resource': resource}
    )
    print(resp, resp.request.url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    for link in data['links']:
        if link.get('rel') == 'self' and link.get('type') == 'application/activity+json':
            return link.get('href')
    return None
