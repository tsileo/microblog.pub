import base64
import mimetypes
from enum import Enum
from enum import unique
from functools import lru_cache
from gzip import GzipFile
from io import BytesIO
from typing import Any
from typing import Dict
from typing import Optional
from typing import Tuple

import gridfs
import piexif
import requests
from little_boxes import activitypub as ap
from PIL import Image


@lru_cache(2048)
def _is_img(filename):
    mimetype, _ = mimetypes.guess_type(filename.lower())
    if mimetype and mimetype.split("/")[0] in ["image"]:
        return True
    return False


@lru_cache(2048)
def is_video(filename):
    mimetype, _ = mimetypes.guess_type(filename.lower())
    if mimetype and mimetype.split("/")[0] in ["video"]:
        return True
    return False


def _load(url: str, user_agent: str) -> Tuple[BytesIO, Optional[str]]:
    """Initializes a `PIL.Image` from the URL."""
    out = BytesIO()
    with requests.get(url, stream=True, headers={"User-Agent": user_agent}) as resp:
        resp.raise_for_status()

        resp.raw.decode_content = True
        while 1:
            buf = resp.raw.read()
            if not buf:
                break
            out.write(buf)
    out.seek(0)
    return out, resp.headers.get("content-type")


def load(url: str, user_agent: str) -> Image:
    """Initializes a `PIL.Image` from the URL."""
    out, _ = _load(url, user_agent)
    return Image.open(out)


def to_data_uri(img: Image) -> str:
    out = BytesIO()
    img.save(out, format=img.format)
    out.seek(0)
    data = base64.b64encode(out.read()).decode("utf-8")
    return f"data:{img.get_format_mimetype()};base64,{data}"


@unique
class Kind(Enum):
    ATTACHMENT = "attachment"
    ACTOR_ICON = "actor_icon"
    UPLOAD = "upload"
    OG_IMAGE = "og"
    EMOJI = "emoji"


class MediaCache(object):
    def __init__(self, gridfs_db: str, user_agent: str) -> None:
        self.fs = gridfs.GridFS(gridfs_db)
        self.user_agent = user_agent

    def cache_og_image(self, url: str, remote_id: str) -> None:
        if self.fs.find_one({"url": url, "kind": Kind.OG_IMAGE.value}):
            return
        i = load(url, self.user_agent)
        # Save the original attachment (gzipped)
        i.thumbnail((100, 100))
        with BytesIO() as buf:
            with GzipFile(mode="wb", fileobj=buf) as f1:
                i.save(f1, format=i.format)
            buf.seek(0)
            self.fs.put(
                buf,
                url=url,
                size=100,
                content_type=i.get_format_mimetype(),
                kind=Kind.OG_IMAGE.value,
                remote_id=remote_id,
            )

    def cache_attachment(self, attachment: Dict[str, Any], remote_id: str) -> None:
        url = attachment["url"]

        # Ensure it's not already there
        if self.fs.find_one(
            {"url": url, "kind": Kind.ATTACHMENT.value, "remote_id": remote_id}
        ):
            return

        # If it's an image, make some thumbnails
        if (
            _is_img(url)
            or attachment.get("mediaType", "").startswith("image/")
            or ap._has_type(attachment.get("type"), ap.ActivityType.IMAGE)
        ):
            try:
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
                        remote_id=remote_id,
                    )
                # Save a thumbnail (gzipped)
                i.thumbnail((720, 720))
                with BytesIO() as buf:
                    with GzipFile(mode="wb", fileobj=buf) as f1:
                        i.save(f1, format=i.format)
                    buf.seek(0)
                    self.fs.put(
                        buf,
                        url=url,
                        size=720,
                        content_type=i.get_format_mimetype(),
                        kind=Kind.ATTACHMENT.value,
                        remote_id=remote_id,
                    )
                return
            except Exception:
                # FIXME(tsileo): logging
                pass

        # The attachment is not an image, download and save it anyway
        with requests.get(
            url, stream=True, headers={"User-Agent": self.user_agent}
        ) as resp:
            resp.raise_for_status()
            with BytesIO() as buf:
                with GzipFile(mode="wb", fileobj=buf) as f1:
                    for chunk in resp.iter_content(chunk_size=2 << 20):
                        if chunk:
                            print(len(chunk))
                            f1.write(chunk)
                buf.seek(0)
                self.fs.put(
                    buf,
                    url=url,
                    size=None,
                    content_type=mimetypes.guess_type(url)[0],
                    kind=Kind.ATTACHMENT.value,
                    remote_id=remote_id,
                )

    def is_actor_icon_cached(self, url: str) -> bool:
        return bool(self.fs.find_one({"url": url, "kind": Kind.ACTOR_ICON.value}))

    def cache_actor_icon(self, url: str) -> None:
        if self.is_actor_icon_cached(url):
            return
        i = load(url, self.user_agent)
        for size in [50, 80]:
            t1 = i.copy()
            t1.thumbnail((size, size))
            with BytesIO() as buf:
                with GzipFile(mode="wb", fileobj=buf) as f1:
                    t1.save(f1, format=i.format)
                buf.seek(0)
                self.fs.put(
                    buf,
                    url=url,
                    size=size,
                    content_type=i.get_format_mimetype(),
                    kind=Kind.ACTOR_ICON.value,
                )

    def is_emoji_cached(self, url: str) -> bool:
        return bool(self.fs.find_one({"url": url, "kind": Kind.EMOJI.value}))

    def cache_emoji(self, url: str, iri: str) -> None:
        if self.is_emoji_cached(url):
            return
        i = load(url, self.user_agent)
        for size in [25]:
            t1 = i.copy()
            t1.thumbnail((size, size))
            with BytesIO() as buf:
                with GzipFile(mode="wb", fileobj=buf) as f1:
                    t1.save(f1, format=i.format)
                buf.seek(0)
                self.fs.put(
                    buf,
                    url=url,
                    size=size,
                    remote_id=iri,
                    content_type=i.get_format_mimetype(),
                    kind=Kind.EMOJI.value,
                )

    def save_upload(self, obuf: BytesIO, filename: str) -> str:
        # Remove EXIF metadata
        if filename.lower().endswith(".jpg") or filename.lower().endswith(".jpeg"):
            obuf.seek(0)
            with BytesIO() as buf2:
                piexif.remove(obuf.getvalue(), buf2)
                obuf.truncate(0)
                obuf.write(buf2.getvalue())

        obuf.seek(0)
        mtype = mimetypes.guess_type(filename)[0]
        with BytesIO() as gbuf:
            with GzipFile(mode="wb", fileobj=gbuf) as gzipfile:
                gzipfile.write(obuf.getvalue())

            gbuf.seek(0)
            oid = self.fs.put(
                gbuf,
                content_type=mtype,
                upload_filename=filename,
                kind=Kind.UPLOAD.value,
            )
            return str(oid)

    def get_actor_icon(self, url: str, size: int) -> Any:
        return self.get_file(url, size, Kind.ACTOR_ICON)

    def get_attachment(self, url: str, size: int) -> Any:
        return self.get_file(url, size, Kind.ATTACHMENT)

    def get_file(self, url: str, size: int, kind: Kind) -> Any:
        return self.fs.find_one({"url": url, "size": size, "kind": kind.value})
