from typing import Optional, Dict, List, Any

import requests

from .errors import RecursionLimitExceededError
from .errors import UnexpectedActivityTypeError


def _do_req(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def parse_collection(
    payload: Optional[Dict[str, Any]] = None,
    url: Optional[str] = None,
    user_agent: Optional[str] = None,
    level: int = 0,
    do_req: Any = _do_req,
) -> List[str]:
    """Resolve/fetch a `Collection`/`OrderedCollection`."""
    if level > 3:
        raise RecursionLimitExceededError('recursion limit exceeded')

    # Go through all the pages
    headers = {'Accept': 'application/activity+json'}
    if user_agent:
        headers['User-Agent'] = user_agent

    out: List[str] = []
    if url:
        payload = do_req(url, headers) 
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
                out.extend(parse_collection(url=n, user_agent=user_agent, level=level+1, do_req=do_req))
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
            payload = do_req(n, headers)
        else:
            raise UnexpectedActivityTypeError('unexpected activity type {}'.format(payload['type']))

    return out
