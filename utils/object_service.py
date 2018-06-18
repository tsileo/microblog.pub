import logging

from little_boxes.activitypub import get_backend

logger = logging.getLogger(__name__)


class ObjectService(object):
    def __init__(self):
        logger.debug("Initializing ObjectService")
        self._cache = {}

    def get(self, iri, reload_cache=False):
        logger.info(f"get actor {iri} (reload_cache={reload_cache})")

        if not reload_cache and iri in self._cache:
            return self._cache[iri]

        obj = get_backend().fetch_iri(iri)
        self._cache[iri] = obj
        return obj
