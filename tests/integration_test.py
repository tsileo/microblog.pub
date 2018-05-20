import requests

def test_ping_homepage():
    """Ensure the homepage is accessible."""
    resp = requests.get('http://localhost:5005')
    resp.raise_for_status()
    assert 'ci@localhost' in resp.text
