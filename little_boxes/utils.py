"""Contains some ActivityPub related utils."""
from typing import Optional
from typing import Callable
from typing import Dict
from typing import List
from typing import Any


from .errors import RecursionLimitExceededError
from .errors import UnexpectedActivityTypeError


def parse_collection(
    payload: Optional[Dict[str, Any]] = None,
    url: Optional[str] = None,
    level: int = 0,
    fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> List[Any]:
    """Resolve/fetch a `Collection`/`OrderedCollection`."""
    if not fetcher:
        raise Exception('must provide a fetcher')
    if level > 3:
        raise RecursionLimitExceededError('recursion limit exceeded')

    # Go through all the pages
    out: List[Any] = []
    if url:
        payload = fetcher(url)
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
                out.extend(parse_collection(url=n, level=level+1, fetcher=fetcher))
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
            payload = fetcher(n)
        else:
            raise UnexpectedActivityTypeError('unexpected activity type {}'.format(payload['type']))

    return out
