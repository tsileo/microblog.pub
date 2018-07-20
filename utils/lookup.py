import json

import little_boxes.activitypub as ap
import mf2py
import requests


def lookup(url: str) -> ap.BaseActivity:
    """Try to find an AP object related to the given URL."""
    backend = ap.get_backend()
    resp = requests.get(
        url,
        timeout=15,
        allow_redirects=False,
        headers={"User-Agent": backend.user_agent()},
    )
    resp.raise_for_status()

    # If the page is HTML, maybe it contains an alternate link pointing to an AP object
    for alternate in mf2py.parse(resp.text).get("alternates", []):
        if alternate.get("type") == "application/activity+json":
            return ap.fetch_remote_activity(alternate["url"])

    try:
        # Maybe the page was JSON-LD?
        data = resp.json()
        return ap.parse_activity(data)
    except json.JSONDecodeError:
        pass

    # Try content negotiation (retry with the AP Accept header)
    return ap.fetch_remote_activity(url)
