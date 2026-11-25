"""HTTP clients that apply the SSRF protection to every request.

`check_url` alone only validates the URL it is given; when a client follows
redirects, the next hops are never checked. These clients run `check_url` in a
request event hook, which httpx calls before sending the initial request *and*
each redirect hop, so a public URL redirecting to an internal address is
rejected.

Every outgoing request to a remote/untrusted URL must use these clients.
"""
from typing import Any

import httpx

from app.utils.url import InvalidURLError
from app.utils.url import check_url


class BlockedURLError(InvalidURLError, httpx.RequestError):
    """A request (or a redirect hop) targeted a URL rejected by `check_url`.

    It is both an `InvalidURLError` and an `httpx.HTTPError`, so existing
    `except httpx.HTTPError` handlers treat it as a failed fetch.
    """


def _check_request(request: httpx.Request) -> None:
    try:
        check_url(str(request.url))
    except InvalidURLError as exc:
        raise BlockedURLError(str(exc), request=request) from exc
    except OSError as exc:
        # DNS resolution failure: surface it like httpx would (a ConnectError)
        # so callers handling `httpx.HTTPError` keep working
        raise httpx.ConnectError(str(exc), request=request) from exc


async def _async_check_request(request: httpx.Request) -> None:
    _check_request(request)


def _with_hook(kwargs: dict[str, Any], hook: Any) -> dict[str, Any]:
    event_hooks = dict(kwargs.pop("event_hooks", None) or {})
    event_hooks["request"] = [hook, *event_hooks.get("request", [])]
    return {**kwargs, "event_hooks": event_hooks}


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """`httpx.AsyncClient` validating every request URL, redirects included."""
    return httpx.AsyncClient(**_with_hook(kwargs, _async_check_request))


def client(**kwargs: Any) -> httpx.Client:
    """`httpx.Client` validating every request URL, redirects included."""
    return httpx.Client(**_with_hook(kwargs, _check_request))
