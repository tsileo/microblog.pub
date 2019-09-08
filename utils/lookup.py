import little_boxes.activitypub as ap
import mf2py
import requests
from little_boxes.errors import NotAnActivityError
from little_boxes.errors import RemoteServerUnavailableError
from little_boxes.webfinger import get_actor_url


def lookup(url: str) -> ap.BaseActivity:
    """Try to find an AP object related to the given URL."""
    try:
        if url.startswith("@"):
            actor_url = get_actor_url(url)
            if actor_url:
                return ap.fetch_remote_activity(actor_url)
    except NotAnActivityError:
        pass
    except requests.HTTPError:
        # Some websites may returns 404, 503 or others when they don't support webfinger, and we're just taking a guess
        # when performing the lookup.
        pass
    except requests.RequestException as err:
        raise RemoteServerUnavailableError(f"failed to fetch {url}: {err!r}")

    backend = ap.get_backend()
    try:
        resp = requests.head(
            url,
            timeout=10,
            allow_redirects=True,
            headers={"User-Agent": backend.user_agent()},
        )
    except requests.RequestException as err:
        raise RemoteServerUnavailableError(f"failed to GET {url}: {err!r}")

    try:
        resp.raise_for_status()
    except Exception:
        return ap.fetch_remote_activity(url)

    # If the page is HTML, maybe it contains an alternate link pointing to an AP object
    for alternate in mf2py.parse(resp.text).get("alternates", []):
        if alternate.get("type") == "application/activity+json":
            return ap.fetch_remote_activity(alternate["url"])

    try:
        # Maybe the page was JSON-LD?
        data = resp.json()
        return ap.parse_activity(data)
    except Exception:
        pass

    # Try content negotiation (retry with the AP Accept header)
    return ap.fetch_remote_activity(url)
