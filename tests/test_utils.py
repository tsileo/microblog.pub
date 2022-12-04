from unittest import mock

import pytest

from app.utils.url import is_hostname_blocked


@pytest.mark.parametrize(
    "hostname,should_be_blocked",
    [
        ("example.com", True),
        ("subdomain.example.com", True),
        ("example.xyz", False),
    ],
)
def test_is_hostname_blocked(hostname: str, should_be_blocked: bool) -> None:
    with mock.patch("app.utils.url.BLOCKED_SERVERS", ["example.com"]):
        is_hostname_blocked.cache_clear()
        assert is_hostname_blocked(hostname) is should_be_blocked
