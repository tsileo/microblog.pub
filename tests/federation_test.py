import time
import os

import requests
from html2text import html2text
from utils import activitypub_utils


def resp2plaintext(resp):
    """Convert the body of a requests reponse to plain text in order to make basic assertions."""
    return html2text(resp.text)


class Instance(object):
    """Test instance wrapper."""

    def __init__(self, name, host_url, docker_url=None):
        self.host_url = host_url
        self.docker_url = docker_url or host_url
        self.session = requests.Session()
        self._create_delay = 10
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), f'fixtures/{name}/config/admin_api_key.key')) as f:
            api_key = f.read()
        self._auth_headers = {'Authorization': f'Bearer {api_key}'}

    def _do_req(self, url, headers):
        url = url.replace(self.docker_url, self.host_url)
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def _parse_collection(self, payload=None, url=None):
        return activitypub_utils.parse_collection(url=url, payload=payload, do_req=self._do_req)

    def ping(self):
        """Ensures the homepage is reachable."""
        resp = self.session.get(f'{self.host_url}/')
        resp.raise_for_status()
        assert resp.status_code == 200

    def debug(self):
        resp = self.session.get(f'{self.host_url}/api/debug', headers={'Accept': 'application/json'})
        resp.raise_for_status()

        return resp.json()
    
    def drop_db(self):
        resp = self.session.delete(f'{self.host_url}/api/debug', headers={'Accept': 'application/json'})
        resp.raise_for_status()

        return resp.json()

    def login(self):
        resp = self.session.post(f'{self.host_url}/login', data={'pass': 'hello'})
        resp.raise_for_status()
        assert resp.status_code == 200

    def block(self, actor_url) -> None:
        # Instance1 follows instance2
        resp = self.session.get(f'{self.host_url}/api/block', params={'actor': actor_url})
        assert resp.status_code == 201

        # We need to wait for the Follow/Accept dance
        time.sleep(self._create_delay/2)
        return resp.headers.get('microblogpub-created-activity')

    def follow(self, instance: 'Instance') -> None:
        # Instance1 follows instance2
        resp = self.session.get(f'{self.host_url}/api/follow', params={'actor': instance.docker_url})
        assert resp.status_code == 201

        # We need to wait for the Follow/Accept dance
        time.sleep(self._create_delay)
        return resp.headers.get('microblogpub-created-activity')

    def new_note(self, content, reply=None):
        params = {'content': content}
        if reply:
            params['reply'] = reply
        resp = self.session.get(f'{self.host_url}/api/new_note', params=params)
        assert resp.status_code == 201

        time.sleep(self._create_delay)
        return resp.headers.get('microblogpub-created-activity')

    def boost(self, activity_id):
        resp = self.session.get(f'{self.host_url}/api/boost', params={'id': activity_id})
        assert resp.status_code == 201

        time.sleep(self._create_delay)
        return resp.headers.get('microblogpub-created-activity')

    def like(self, activity_id):
        resp = self.session.get(f'{self.host_url}/api/like', params={'id': activity_id})
        assert resp.status_code == 201

        time.sleep(self._create_delay)
        return resp.headers.get('microblogpub-created-activity')

    def delete(self, oid: str) -> None:
        resp = requests.post(
                f'{self.host_url}/api/note/delete',
                json={'id': oid},
                headers=self._auth_headers,        
        )
        assert resp.status_code == 201

        time.sleep(self._create_delay)
        return resp.json().get('activity')

    def undo(self, oid: str) -> None:
        resp = self.session.get(f'{self.host_url}/api/undo', params={'id': oid})
        assert resp.status_code == 201

        # We need to wait for the Follow/Accept dance
        time.sleep(self._create_delay)
        return resp.headers.get('microblogpub-created-activity')

    def followers(self):
        resp = self.session.get(f'{self.host_url}/followers', headers={'Accept': 'application/activity+json'})
        resp.raise_for_status()

        data = resp.json()

        return self._parse_collection(payload=data)

    def following(self):
        resp = self.session.get(f'{self.host_url}/following', headers={'Accept': 'application/activity+json'})
        resp.raise_for_status()

        data = resp.json()

        return self._parse_collection(payload=data)

    def outbox(self):
        resp = self.session.get(f'{self.host_url}/following', headers={'Accept': 'application/activity+json'})
        resp.raise_for_status()
        return resp.json()

    def outbox_get(self, aid):
        resp = self.session.get(aid.replace(self.docker_url, self.host_url), headers={'Accept': 'application/activity+json'})
        resp.raise_for_status()
        return resp.json()

    def stream_jsonfeed(self):
        resp = self.session.get(f'{self.host_url}/api/stream', headers={'Accept': 'application/json'})
        resp.raise_for_status()
        return resp.json()


def _instances():
    instance1 = Instance('instance1', 'http://localhost:5006', 'http://instance1_web_1:5005')
    instance1.ping()

    instance2 = Instance('instance2', 'http://localhost:5007', 'http://instance2_web_1:5005')
    instance2.ping()

    # Login
    instance1.login()
    instance1.drop_db()
    instance2.login()
    instance2.drop_db()
    
    return instance1, instance2


def test_follow():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 1  # An Accept activity should be there
    assert instance1_debug['outbox'] == 1  # We've sent a Follow activity

    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 1  # An Follow activity should be there
    assert instance2_debug['outbox'] == 1  # We've sent a Accept activity

    assert instance2.followers() == [instance1.docker_url]
    assert instance1.following() == [instance2.docker_url]


def test_follow_unfollow():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    follow_id = instance1.follow(instance2)
    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 1  # An Accept activity should be there
    assert instance1_debug['outbox'] == 1  # We've sent a Follow activity

    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 1  # An Follow activity should be there
    assert instance2_debug['outbox'] == 1  # We've sent a Accept activity

    assert instance2.followers() == [instance1.docker_url]
    assert instance1.following() == [instance2.docker_url]

    instance1.undo(follow_id)

    assert instance2.followers() == []
    assert instance1.following() == []

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 1  # An Accept activity should be there
    assert instance1_debug['outbox'] == 2  # We've sent a Follow and a Undo activity

    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 2  # An Follow and Undo activity should be there
    assert instance2_debug['outbox'] == 1  # We've sent a Accept activity


def test_post_content():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 0

    create_id = instance1.new_note('hello')
    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 3  # An Follow, Accept and Create activity should be there
    assert instance2_debug['outbox'] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 1
    assert inbox_stream['items'][0]['id'] == create_id


def test_block_and_post_content():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 0

    instance2.block(instance1.docker_url)

    instance1.new_note('hello')
    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 2  # An Follow, Accept activity should be there, Create should have been dropped
    assert instance2_debug['outbox'] == 3  # We've sent a Accept and a Follow  activity + the Block activity

    # Ensure the post is not visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 0


def test_post_content_and_delete():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 0

    create_id = instance1.new_note('hello')
    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 3  # An Follow, Accept and Create activity should be there
    assert instance2_debug['outbox'] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 1
    assert inbox_stream['items'][0]['id'] == create_id

    instance1.delete(f'{create_id}/activity')
    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 4  # An Follow, Accept and Create and Delete activity should be there
    assert instance2_debug['outbox'] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post has been delete from instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 0


def test_post_content_and_like():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    create_id = instance1.new_note('hello')

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 1
    assert inbox_stream['items'][0]['id'] == create_id

    # Now, instance2 like the note
    like_id = instance2.like(f'{create_id}/activity')

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 3  # Follow, Accept and Like
    assert instance1_debug['outbox'] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f'{create_id}/activity')
    assert 'likes' in note
    assert note['likes']['totalItems'] == 1
    # assert note['likes']['items'][0]['id'] == like_id


def test_post_content_and_like_unlike():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    create_id = instance1.new_note('hello')

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 1
    assert inbox_stream['items'][0]['id'] == create_id

    # Now, instance2 like the note
    like_id = instance2.like(f'{create_id}/activity')

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 3  # Follow, Accept and Like
    assert instance1_debug['outbox'] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f'{create_id}/activity')
    assert 'likes' in note
    assert note['likes']['totalItems'] == 1
    # FIXME(tsileo): parse the collection
    # assert note['likes']['items'][0]['id'] == like_id

    instance2.undo(like_id)

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 4  # Follow, Accept and Like and Undo
    assert instance1_debug['outbox'] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f'{create_id}/activity')
    assert 'likes' in note
    assert note['likes']['totalItems'] == 0


def test_post_content_and_boost():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    create_id = instance1.new_note('hello')

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 1
    assert inbox_stream['items'][0]['id'] == create_id

    # Now, instance2 like the note
    boost_id = instance2.boost(f'{create_id}/activity')

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 3  # Follow, Accept and Announce
    assert instance1_debug['outbox'] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f'{create_id}/activity')
    assert 'shares' in note
    assert note['shares']['totalItems'] == 1
    # FIXME(tsileo): parse the collection
    # assert note['shares']['items'][0]['id'] == boost_id


def test_post_content_and_boost_unboost():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    create_id = instance1.new_note('hello')

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 1
    assert inbox_stream['items'][0]['id'] == create_id

    # Now, instance2 like the note
    boost_id = instance2.boost(f'{create_id}/activity')

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 3  # Follow, Accept and Announce
    assert instance1_debug['outbox'] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f'{create_id}/activity')
    assert 'shares' in note
    assert note['shares']['totalItems'] == 1
    # FIXME(tsileo): parse the collection
    # assert note['shares']['items'][0]['id'] == boost_id

    instance2.undo(boost_id)

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 4  # Follow, Accept and Announce and Undo
    assert instance1_debug['outbox'] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f'{create_id}/activity')
    assert 'shares' in note
    assert note['shares']['totalItems'] == 0


def test_post_content_and_post_reply():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 0

    instance1_create_id = instance1.new_note('hello')
    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 3  # An Follow, Accept and Create activity should be there
    assert instance2_debug['outbox'] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post is visible in instance2's stream
    instance2_inbox_stream = instance2.stream_jsonfeed()
    assert len(instance2_inbox_stream['items']) == 1
    assert instance2_inbox_stream['items'][0]['id'] == instance1_create_id

    instance2_create_id = instance2.new_note(f'hey @instance1@{instance1.docker_url}', reply=f'{instance1_create_id}/activity')
    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 3  # An Follow, Accept and Create activity should be there
    assert instance2_debug['outbox'] == 3  # We've sent a Accept and a Follow and a Create  activity

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 3  # An Follow, Accept and Create activity should be there
    assert instance1_debug['outbox'] == 3  # We've sent a Accept and a Follow and a Create  activity

    instance1_inbox_stream = instance1.stream_jsonfeed()
    assert len(instance1_inbox_stream['items']) == 1
    assert instance1_inbox_stream['items'][0]['id'] == instance2_create_id

    instance1_note = instance1.outbox_get(f'{instance1_create_id}/activity')
    assert 'replies' in instance1_note
    assert instance1_note['replies']['totalItems'] == 1
    # TODO(tsileo): inspect the `replies` collection


def test_post_content_and_post_reply_and_delete():
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream['items']) == 0

    instance1_create_id = instance1.new_note('hello')
    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 3  # An Follow, Accept and Create activity should be there
    assert instance2_debug['outbox'] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post is visible in instance2's stream
    instance2_inbox_stream = instance2.stream_jsonfeed()
    assert len(instance2_inbox_stream['items']) == 1
    assert instance2_inbox_stream['items'][0]['id'] == instance1_create_id

    instance2_create_id = instance2.new_note(f'hey @instance1@{instance1.docker_url}', reply=f'{instance1_create_id}/activity')
    instance2_debug = instance2.debug()
    assert instance2_debug['inbox'] == 3  # An Follow, Accept and Create activity should be there
    assert instance2_debug['outbox'] == 3  # We've sent a Accept and a Follow and a Create  activity

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 3  # An Follow, Accept and Create activity should be there
    assert instance1_debug['outbox'] == 3  # We've sent a Accept and a Follow and a Create  activity

    instance1_inbox_stream = instance1.stream_jsonfeed()
    assert len(instance1_inbox_stream['items']) == 1
    assert instance1_inbox_stream['items'][0]['id'] == instance2_create_id

    instance1_note = instance1.outbox_get(f'{instance1_create_id}/activity')
    assert 'replies' in instance1_note
    assert instance1_note['replies']['totalItems'] == 1

    instance2.delete(f'{instance2_create_id}/activity')

    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 4  # An Follow, Accept and Create and Delete activity should be there
    assert instance1_debug['outbox'] == 3  # We've sent a Accept and a Follow and a Create  activity

    instance1_note = instance1.outbox_get(f'{instance1_create_id}/activity')
    assert 'replies' in instance1_note
    assert instance1_note['replies']['totalItems'] == 0
