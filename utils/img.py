import base64
from gzip import GzipFile
from io import BytesIO
from typing import Any

import gridfs
import requests
from PIL import Image


def load(url):
    """Initializes a `PIL.Image` from the URL."""
    # TODO(tsileo): user agent
    resp = requests.get(url, stream=True)
    resp.raise_for_status()
    try:
        image = Image.open(BytesIO(resp.raw.read()))
    finally:
        resp.close()
    return image


def to_data_uri(img):
    out = BytesIO()
    img.save(out, format=img.format)
    out.seek(0)
    data = base64.b64encode(out.read()).decode("utf-8")
    return f"data:{img.get_format_mimetype()};base64,{data}"


class ImageCache(object):
    def __init__(self, gridfs_db: str) -> None:
        self.fs = gridfs.GridFS(gridfs_db)

    def cache_actor_icon(self, url: str):
        if self.fs.find_one({"url": url}):
            return
        i = load(url)
        for size in [50, 80]:
            t1 = i.copy()
            t1.thumbnail((size, size))
            with BytesIO() as buf:
                f1 = GzipFile(mode='wb', fileobj=buf)
                t1.save(f1, format=i.format)
                f1.close()
                buf.seek(0)
                self.fs.put(
                    buf, url=url, size=size, content_type=i.get_format_mimetype()
                )

    def get_file(self, url: str, size: int) -> Any:
        return self.fs.find_one({"url": url, "size": size})
