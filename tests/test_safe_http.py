from typing import Generator
from unittest import mock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import activitypub as ap
from app import media
from app.utils import safe_http
from app.utils import url as url_utils
from app.utils.microformats import fetch_and_parse

_FAKE_DNS = {
    "public.example": "93.184.216.34",
    "internal.example": "10.0.0.5",
}


def _fake_getaddrinfo(hostname: str, port: int) -> str:
    try:
        return _FAKE_DNS[hostname]
    except KeyError:
        # Literal IPs
        return hostname


@pytest.fixture
def ssrf_protection_enabled() -> Generator:
    """The test config runs with `debug = true`, which disables the checks."""
    url_utils.check_url.cache_clear()
    with mock.patch.object(url_utils, "DEBUG", False), mock.patch.object(
        url_utils, "_getaddrinfo", _fake_getaddrinfo
    ):
        yield
    url_utils.check_url.cache_clear()


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/admin",
        "http://[::1]/",
        "http://internal.example/",
        "http://localhost/",
    ],
)
@pytest.mark.asyncio
async def test_async_client__blocks_redirect_to_private_address(
    ssrf_protection_enabled: None,
    respx_mock: respx.MockRouter,
    location: str,
) -> None:
    respx_mock.get("https://public.example/start").mock(
        return_value=httpx.Response(302, headers={"Location": location})
    )
    internal_route = respx_mock.route(url__startswith=location).mock(
        return_value=httpx.Response(200, text="secret")
    )

    async with safe_http.async_client() as client:
        with pytest.raises(safe_http.BlockedURLError) as exc_info:
            await client.get("https://public.example/start", follow_redirects=True)

    # Handled by the existing `except httpx.HTTPError` and `except InvalidURLError`
    assert isinstance(exc_info.value, httpx.HTTPError)
    assert isinstance(exc_info.value, url_utils.InvalidURLError)
    assert not internal_route.called


@pytest.mark.asyncio
async def test_async_client__allows_redirect_to_public_address(
    ssrf_protection_enabled: None,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("https://public.example/start").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://93.184.216.34/next"}
        )
    )
    respx_mock.get("https://93.184.216.34/next").mock(
        return_value=httpx.Response(200, text="ok")
    )

    async with safe_http.async_client() as client:
        resp = await client.get("https://public.example/start", follow_redirects=True)

    assert resp.text == "ok"


@pytest.mark.asyncio
async def test_async_client__blocks_initial_private_url(
    ssrf_protection_enabled: None,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get("http://169.254.169.254/").mock(
        return_value=httpx.Response(200)
    )

    async with safe_http.async_client() as client:
        with pytest.raises(safe_http.BlockedURLError):
            await client.get("http://169.254.169.254/")

    assert not route.called


def test_client__blocks_redirect_to_private_address(
    ssrf_protection_enabled: None,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("https://public.example/start").mock(
        return_value=httpx.Response(
            302, headers={"Location": "http://169.254.169.254/"}
        )
    )
    internal_route = respx_mock.get("http://169.254.169.254/").mock(
        return_value=httpx.Response(200)
    )

    with safe_http.client() as client:
        with pytest.raises(safe_http.BlockedURLError):
            client.get("https://public.example/start", follow_redirects=True)

    assert not internal_route.called


@pytest.mark.asyncio
async def test_ap_fetch__blocks_redirect_to_private_address(
    ssrf_protection_enabled: None,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("https://public.example/actor").mock(
        return_value=httpx.Response(
            302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
        )
    )
    internal_route = respx_mock.get("http://169.254.169.254/latest/meta-data/").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(safe_http.BlockedURLError):
        await ap.fetch("https://public.example/actor")

    assert not internal_route.called


@pytest.mark.asyncio
async def test_microformats_fetch__blocks_redirect_to_private_address(
    ssrf_protection_enabled: None,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("https://public.example/post").mock(
        return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/"})
    )
    internal_route = respx_mock.get("http://127.0.0.1:8000/").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )

    # Handled by the webmention endpoint's `except httpx.HTTPError`
    with pytest.raises(httpx.HTTPError):
        await fetch_and_parse("https://public.example/post")

    assert not internal_route.called


def _proxy_path(url: str) -> str:
    return media.proxied_media_url(url).removeprefix(media.BASE_URL)


def test_proxy_media__blocks_redirect_to_private_address(
    client: TestClient,
    ssrf_protection_enabled: None,
    respx_mock: respx.MockRouter,
) -> None:
    attachment_url = "https://public.example/image.png"
    respx_mock.get(attachment_url).mock(
        return_value=httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/computeMetadata/v1/token"},
        )
    )
    internal_route = respx_mock.get(
        "http://169.254.169.254/computeMetadata/v1/token"
    ).mock(return_value=httpx.Response(200, text="secret-token"))

    with pytest.raises(safe_http.BlockedURLError):
        client.get(_proxy_path(attachment_url))

    assert not internal_route.called


def test_proxy_media__only_forwards_allowed_headers(
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    attachment_url = "https://public.example/video.mp4"
    route = respx_mock.get(attachment_url).mock(
        return_value=httpx.Response(200, content=b"data")
    )

    resp = client.get(
        _proxy_path(attachment_url),
        headers={
            "Range": "bytes=0-1",
            "Metadata-Flavor": "Google",
            "Metadata": "true",
            "Authorization": "Bearer abc",
            "X-Custom": "1",
        },
    )

    assert resp.status_code == 200
    upstream_call = route.calls.last
    assert upstream_call is not None
    upstream_headers = upstream_call.request.headers
    assert upstream_headers["range"] == "bytes=0-1"
    for header in ["metadata-flavor", "metadata", "authorization", "x-custom"]:
        assert header not in upstream_headers
