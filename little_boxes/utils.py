"""Contains some ActivityPub related utils."""
from typing import Optional
from typing import Dict
from typing import List
from typing import Any

import requests

from .errors import RecursionLimitExceededError
from .errors import UnexpectedActivityTypeError
from .remote_object import OBJECT_FETCHER


def parse_collection(
    payload: Optional[Dict[str, Any]] = None,
    url: Optional[str] = None,
    level: int = 0,
) -> List[Any]:
    """Resolve/fetch a `Collection`/`OrderedCollection`."""
    if level > 3:
        raise RecursionLimitExceededError('recursion limit exceeded')

    # Go through all the pages
    headers = {'Accept': 'application/activity+json'}
    if user_agent:
        headers['User-Agent'] = user_agent

    out: List[Any] = []
    if url:
        payload = OBJECT_FETCHER.fetch(url)
    if not payload:
        raise ValueError('must at least prove a payload or an URL')

    if payload['type'] in ['Collection', 'OrderedCollection']:
        if 'orderedItems' in payload:
            return payload['orderedItems']
        if 'items' in payload:
            return payload['items']
        if 'first' in payload:
            if 'orderedItems' in payload['first']:
                out.extend(payload['first']['orderedItems'])
            if 'items' in payload['first']:
                out.extend(payload['first']['items'])
            n = payload['first'].get('next')
            if n:
                out.extend(parse_collection(url=n, level=level+1))
        return out

    while payload:
        if payload['type'] in ['CollectionPage', 'OrderedCollectionPage']:
            if 'orderedItems' in payload:
                out.extend(payload['orderedItems'])
            if 'items' in payload:
                out.extend(payload['items'])
            n = payload.get('next')
            if n is None:
                break
            payload = OBJECT_FETCHER.fetch(n)
        else:
            raise UnexpectedActivityTypeError('unexpected activity type {}'.format(payload['type']))

    return out
