import base64
import time

from app.config import BASE_URL
from app.config import hmac_sha256

SUPPORTED_RESIZE = [50, 740]
EXPIRY_PERIOD = 86400
EXPIRY_LENGTH = 7


class InvalidProxySignatureError(Exception):
    pass


def proxied_media_sig(expires: int, url: str) -> str:
    hm = hmac_sha256()
    hm.update(f"{expires}".encode())
    hm.update(b"|")
    hm.update(url.encode())
    return base64.urlsafe_b64encode(hm.digest()).decode()


def verify_proxied_media_sig(expires: int, url: str, sig: str) -> None:
    now = int(time.time() / EXPIRY_PERIOD)
    expected = proxied_media_sig(expires, url)
    if now > expires or sig != expected:
        raise InvalidProxySignatureError("invalid or expired media")


def proxied_media_url(url: str) -> str:
    if url.startswith(BASE_URL):
        return url
    expires = int(time.time() / EXPIRY_PERIOD) + EXPIRY_LENGTH
    sig = proxied_media_sig(expires, url)

    return (
        BASE_URL
        + f"/proxy/media/{expires}/{sig}/"
        + base64.urlsafe_b64encode(url.encode()).decode()
    )


def resized_media_url(url: str, size: int) -> str:
    if size not in SUPPORTED_RESIZE:
        raise ValueError(f"Unsupported resize {size}")
    if url.startswith(BASE_URL):
        return url
    return proxied_media_url(url) + f"/{size}"
