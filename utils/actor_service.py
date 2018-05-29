import logging

import requests
from urllib.parse import urlparse
from Crypto.PublicKey import RSA

from .urlutils import check_url
from .errors import ActivityNotFoundError

logger = logging.getLogger(__name__)


class NotAnActorError(Exception):
    def __init__(self, activity):
        self.activity = activity


class ActorService(object):
    def __init__(self, user_agent, col, actor_id, actor_data, instances):
        logger.debug(f'Initializing ActorService user_agent={user_agent}')
        self._user_agent = user_agent
        self._col = col
        self._in_mem = {actor_id: actor_data}
        self._instances = instances
        self._known_instances = set()

    def _fetch(self, actor_url):
        logger.debug(f'fetching remote object {actor_url}')

        check_url(actor_url)

        resp = requests.get(actor_url, headers={
            'Accept': 'application/activity+json',
            'User-Agent': self._user_agent,    
        })
        if resp.status_code == 404:
            raise ActivityNotFoundError(f'{actor_url} cannot be fetched, 404 not found error')

        resp.raise_for_status()
        return resp.json()

    def get(self, actor_url, reload_cache=False):
        logger.info(f'get actor {actor_url} (reload_cache={reload_cache})')

        if actor_url in self._in_mem:
            return self._in_mem[actor_url]

        instance = urlparse(actor_url)._replace(path='', query='', fragment='').geturl()
        if instance not in self._known_instances:
            self._known_instances.add(instance)
            if not self._instances.find_one({'instance': instance}):
                self._instances.insert({'instance': instance, 'first_object': actor_url})

        if reload_cache:
            actor = self._fetch(actor_url)
            self._in_mem[actor_url] = actor
            self._col.update({'actor_id': actor_url}, {'$set': {'cached_response': actor}}, upsert=True)
            return actor

        cached_actor = self._col.find_one({'actor_id': actor_url})
        if cached_actor:
            return cached_actor['cached_response']

        actor = self._fetch(actor_url)
        if not 'type' in actor:
            raise NotAnActorError(None)
        if actor['type'] != 'Person':
            raise NotAnActorError(actor)

        self._col.update({'actor_id': actor_url}, {'$set': {'cached_response': actor}}, upsert=True)
        self._in_mem[actor_url] = actor
        return actor

    def get_public_key(self, actor_url, reload_cache=False):
        profile = self.get(actor_url, reload_cache=reload_cache)
        pub = profile['publicKey']
        return pub['id'], RSA.importKey(pub['publicKeyPem'])

    def get_inbox_url(self, actor_url, reload_cache=False):
        profile = self.get(actor_url, reload_cache=reload_cache)
        return profile.get('inbox')
