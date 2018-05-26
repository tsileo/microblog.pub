import time
import os

import requests
from html2text import html2text


def resp2plaintext(resp):
    """Convert the body of a requests reponse to plain text in order to make basic assertions."""
    return html2text(resp.text)


class Instance(object):
    """Test instance wrapper."""

    def __init__(self, host_url, docker_url=None):
        self.host_url = host_url
        self.docker_url = docker_url or host_url
        self.session = requests.Session()

    def ping(self):
        """Ensures the homepage is reachable."""
        resp = self.session.get(f'{self.host_url}/')
        resp.raise_for_status()
        assert resp.status_code == 200

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

        return resp.json()['first']['orderedItems']

    def following(self):
        resp = self.session.get(f'{self.host_url}/following', headers={'Accept': 'application/activity+json'})
        resp.raise_for_status()

        data = resp.json()

        return resp.json()['first']['orderedItems']


def test_federation():
    """Ensure the homepage is accessible."""
    instance1 = Instance('http://localhost:5006', 'http://instance1_web_1:5005')
    instance1.ping()

    instance2 = Instance('http://localhost:5007', 'http://instance2_web_1:5005')
    instance2.ping()

    # Login
    instance1.login()
    instance2.login()

    # Instance1 follows instance2
    instance1.follow(instance2)

    assert instance2.followers() == [instance1.docker_url]
    assert instance1.following() == [instance2.docker_url]
