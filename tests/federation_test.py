import time
import os

import requests
from html2text import html2text


def resp2plaintext(resp):
    """Convert the body of a requests reponse to plain text in order to make basic assertions."""
    return html2text(resp.text)


def test_federation():
    """Ensure the homepage is accessible."""
    resp = requests.get('http://localhost:5006')
    resp.raise_for_status()
    assert resp.status_code == 200

    resp = requests.get('http://localhost:5007')
    resp.raise_for_status()
    assert resp.status_code == 200

    # Keep one session per instance

    # Login
    session1 = requests.Session()
    resp = session1.post('http://localhost:5006/login', data={'pass': 'hello'})
    assert resp.status_code == 200

    # Login
    session2 = requests.Session()
    resp = session2.post('http://localhost:5007/login', data={'pass': 'hello'})
    assert resp.status_code == 200

    # Instance1 follows instance2
    resp = session1.get('http://localhost:5006/api/follow', params={'actor': 'http://instance2_web_1:5005'})
    assert resp.status_code == 201


    time.sleep(2)
    resp = requests.get('http://localhost:5007/followers', headers={'Accept': 'application/activity+json'})
    resp.raise_for_status()

    print(resp.json())

    assert resp.json()['first']['orderedItems'] == ['http://instance1_web_1:5005']
