import base64
from gzip import GzipFile
from io import BytesIO
from typing import Any
import mimetypes
from enum import Enum

import gridfs
import requests
from PIL import Image


def load(url, user_agent):
    """Initializes a `PIL.Image` from the URL."""
    # TODO(tsileo): user agent
    with requests.get(url, stream=True, headers={"User-Agent": user_agent}) as resp:
        resp.raise_for_status()
        return Image.open(BytesIO(resp.raw.read()))


def to_data_uri(img):
    out = BytesIO()
    img.save(out, format=img.format)
    out.seek(0)
    data = base64.b64encode(out.read()).decode("utf-8")
    return f"data:{img.get_format_mimetype()};base64,{data}"


class Kind(Enum):
    ATTACHMENT = "attachment"
    ACTOR_ICON = "actor_icon"


class ImageCache(object):
    def __init__(self, gridfs_db: str, user_agent: str) -> None:
        self.fs = gridfs.GridFS(gridfs_db)
        self.user_agent = user_agent

    def cache_attachment(self, url: str) -> None:
        if self.fs.find_one({"url": url, "kind": Kind.ATTACHMENT.value}):
            return
        if (
            url.endswith(".png")
            or url.endswith(".jpg")
            or url.endswith(".jpeg")
            or url.endswith(".gif")
        ):
            i = load(url, self.user_agent)
            # Save the original attachment (gzipped)
            with BytesIO() as buf:
                f1 = GzipFile(mode="wb", fileobj=buf)
                i.save(f1, format=i.format)
                f1.close()
                buf.seek(0)
                self.fs.put(
                    buf,
                    url=url,
                    size=None,
                    content_type=i.get_format_mimetype(),
                    kind=Kind.ATTACHMENT.value,
                )
            # Save a thumbnail (gzipped)
            i.thumbnail((720, 720))
            with BytesIO() as buf:
                f1 = GzipFile(mode="wb", fileobj=buf)
                i.save(f1, format=i.format)
                f1.close()
                buf.seek(0)
                self.fs.put(
                    buf,
                    url=url,
                    size=720,
                    content_type=i.get_format_mimetype(),
                    kind=Kind.ATTACHMENT.value,
                )
            return

        # The attachment is not an image, download and save it anyway
        with requests.get(
            url, stream=True, headers={"User-Agent": self.user_agent}
        ) as resp:
            resp.raise_for_status()
            with BytesIO() as buf:
                f1 = GzipFile(mode="wb", fileobj=buf)
                for chunk in resp.iter_content():
                    if chunk:
                        f1.write(chunk)
                f1.close()
                buf.seek(0)
                self.fs.put(
                    buf,
                    url=url,
                    size=None,
                    content_type=mimetypes.guess_type(url)[0],
                    kind=Kind.ATTACHMENT.value,
                )

    def cache_actor_icon(self, url: str) -> None:
        if self.fs.find_one({"url": url, "kind": Kind.ACTOR_ICON.value}):
            return
        i = load(url, self.user_agent)
        for size in [50, 80]:
            t1 = i.copy()
            t1.thumbnail((size, size))
            with BytesIO() as buf:
                f1 = GzipFile(mode="wb", fileobj=buf)
                t1.save(f1, format=i.format)
                f1.close()
                buf.seek(0)
                self.fs.put(
                    buf,
                    url=url,
                    size=size,
                    content_type=i.get_format_mimetype(),
                    kind=Kind.ACTOR_ICON.value,
                )

    def cache(self, url: str, kind: Kind) -> None:
        if kind == Kind.ACTOR_ICON:
            self.cache_actor_icon(url)
        else:
            self.cache_attachment(url)

    def get_actor_icon(self, url: str, size: int) -> Any:
        return self.get_file(url, size, Kind.ACTOR_ICON)

    def get_attachment(self, url: str, size: int) -> Any:
        return self.get_file(url, size, Kind.ATTACHMENT)

    def get_file(self, url: str, size: int, kind: Kind) -> Any:
        return self.fs.find_one({"url": url, "size": size, "kind": kind.value})
