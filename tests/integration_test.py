import os

import pytest
import requests
from html2text import html2text


@pytest.fixture
def config():
    """Return the current config as a dict."""
    import yaml

    with open(
        os.path.join(os.path.dirname(__file__), "..", "config/me.yml"), "rb"
    ) as f:
        yield yaml.load(f)


def resp2plaintext(resp):
    """Convert the body of a requests reponse to plain text in order to make basic assertions."""
    return html2text(resp.text)


def test_ping_homepage(config):
    """Ensure the homepage is accessible."""
    resp = requests.get("http://localhost:5005")
    resp.raise_for_status()
    assert resp.status_code == 200
    body = resp2plaintext(resp)
    assert config["name"] in body
    assert f"@{config['username']}@{config['domain']}" in body
