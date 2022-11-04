import base64

from app.config import BASE_URL

SUPPORTED_RESIZE = [50, 740]


def proxied_media_url(url: str) -> str:
    if url.startswith(BASE_URL):
        return url

    return BASE_URL + "/proxy/media/" + base64.urlsafe_b64encode(url.encode()).decode()


def resized_media_url(url: str, size: int) -> str:
    if size not in SUPPORTED_RESIZE:
        raise ValueError(f"Unsupported resize {size}")
    if url.startswith(BASE_URL):
        return url
    return proxied_media_url(url) + f"/{size}"
