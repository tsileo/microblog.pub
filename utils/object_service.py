import requests
from urllib.parse import urlparse

from .urlutils import check_url
from .errors import ActivityNotFoundError


class ObjectService(object):
    def __init__(self, user_agent, col, inbox, outbox, instances):
        self._user_agent = user_agent
        self._col = col
        self._inbox = inbox
        self._outbox = outbox
        self._instances = instances
        self._known_instances = set()

    def _fetch_remote(self, object_id):
        print(f'fetch remote {object_id}')
        check_url(object_id)
        resp = requests.get(object_id, headers={
            'Accept': 'application/activity+json',
            'User-Agent': self._user_agent,    
        })
        if resp.status_code == 404:
            raise ActivityNotFoundError(f'{object_id} cannot be fetched, 404 error not found')

        resp.raise_for_status()
        return resp.json()

    def _fetch(self, object_id):
        instance = urlparse(object_id)._replace(path='', query='', fragment='').geturl()
        if instance not in self._known_instances:
            self._known_instances.add(instance)
            if not self._instances.find_one({'instance': instance}):
                self._instances.insert({'instance': instance, 'first_object': object_id})

        obj = self._inbox.find_one({'$or': [{'remote_id': object_id}, {'type': 'Create', 'activity.object.id': object_id}]})
        if obj:
            if obj['remote_id'] == object_id:
                return obj['activity']
            return obj['activity']['object']

        obj = self._outbox.find_one({'$or': [{'remote_id': object_id}, {'type': 'Create', 'activity.object.id': object_id}]})
        if obj:
            if obj['remote_id'] == object_id:
                return obj['activity']
            return obj['activity']['object']

        return self._fetch_remote(object_id)

    def get(self, object_id, reload_cache=False, part_of_stream=False, announce_published=None):
        if reload_cache:
            obj = self._fetch(object_id)
            self._col.update({'object_id': object_id}, {'$set': {'cached_object': obj, 'meta.part_of_stream': part_of_stream, 'meta.announce_published': announce_published}}, upsert=True)
            return obj

        cached_object = self._col.find_one({'object_id': object_id})
        if cached_object:
            print(f'ObjectService: {cached_object}')
            return cached_object['cached_object']

        obj = self._fetch(object_id)

        self._col.update({'object_id': object_id}, {'$set': {'cached_object': obj, 'meta.part_of_stream': part_of_stream, 'meta.announce_published': announce_published}}, upsert=True)
        # print(f'ObjectService: {obj}')

        return obj
