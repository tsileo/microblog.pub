import logging
from typing import Any

import requests
from urllib.parse import urlparse
from Crypto.PublicKey import RSA

from .urlutils import check_url
from .errors import ActivityNotFoundError
from .errors import UnexpectedActivityTypeError

logger = logging.getLogger(__name__)


class DefaultRemoteObjectFetcher(object):
    """Not meant to be used on production, a caching layer, and DB shortcut fox inbox/outbox should be hooked."""

    def __init__(self):
        self._user_agent = 'Little Boxes (+https://github.com/tsileo/little_boxes)'

    def fetch(self, iri):
        check_url(iri)

        resp = requests.get(actor_url, headers={
            'Accept': 'application/activity+json',
            'User-Agent': self._user_agent,    
        })

        if resp.status_code == 404:
            raise ActivityNotFoundError(f'{actor_url} cannot be fetched, 404 not found error')

        resp.raise_for_status()
        
        return resp.json()

OBJECT_FETCHER = DefaultRemoteObjectFetcher()

def set_object_fetcher(object_fetcher: Any):
    OBJECT_FETCHER = object_fetcher
