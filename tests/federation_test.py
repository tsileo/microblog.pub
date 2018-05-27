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

    def __init__(self, host_url, docker_url=None):
        self.host_url = host_url
        self.docker_url = docker_url or host_url
        self.session = requests.Session()

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

    def follow(self, instance: 'Instance') -> None:
        # Instance1 follows instance2
        resp = self.session.get(f'{self.host_url}/api/follow', params={'actor': instance.docker_url})
        assert resp.status_code == 201

        # We need to wait for the Follow/Accept dance
        time.sleep(10)

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


def test_federation():
    """Ensure the homepage is accessible."""
    instance1 = Instance('http://localhost:5006', 'http://instance1_web_1:5005')
    instance1.ping()

    instance2 = Instance('http://localhost:5007', 'http://instance2_web_1:5005')
    instance2.ping()

    # Login
    instance1.login()
    instance1.drop_db()
    instance2.login()
    instance2.drop_db()

    # Instance1 follows instance2
    instance1.follow(instance2)
    instance1_debug = instance1.debug()
    assert instance1_debug['inbox'] == 1  # An Accept activity should be there
    assert instance1_debug['outbox'] == 1  # We've sent a Follow activity

    instance2_debug = instance2.debug()
    assert instance1_debug['inbox'] == 1  # An Follow activity should be there
    assert instance1_debug['outbox'] == 1  # We've sent a Accept activity

    assert instance2.followers() == [instance1.docker_url]
    assert instance1.following() == [instance2.docker_url]
