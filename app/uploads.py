import hashlib
from shutil import COPY_BUFSIZE  # type: ignore

import blurhash  # type: ignore
from fastapi import UploadFile
from loguru import logger
from PIL import Image

from app import activitypub as ap
from app import models
from app.config import BASE_URL
from app.config import ROOT_DIR
from app.database import Session

UPLOAD_DIR = ROOT_DIR / "data" / "uploads"


def save_upload(db: Session, f: UploadFile) -> models.Upload:
    # Compute the hash
    h = hashlib.blake2b(digest_size=32)
    while True:
        buf = f.file.read(COPY_BUFSIZE)
        if not buf:
            break
        h.update(buf)

    f.file.seek(0)
    content_hash = h.hexdigest()

    existing_upload = (
        db.query(models.Upload)
        .filter(models.Upload.content_hash == content_hash)
        .one_or_none()
    )
    if existing_upload:
        logger.info(f"Upload with {content_hash=} already exists")
        return existing_upload

    logger.info(f"Creating new Upload with {content_hash=}")
    dest_filename = UPLOAD_DIR / content_hash
    with open(dest_filename, "wb") as dest:
        while True:
            buf = f.file.read(COPY_BUFSIZE)
            if not buf:
                break
            dest.write(buf)

    has_thumbnail = False
    image_blurhash = None
    width = None
    height = None

    if f.content_type.startswith("image"):
        with open(dest_filename, "rb") as df:
            image_blurhash = blurhash.encode(df, x_components=4, y_components=3)

        try:
            with Image.open(dest_filename) as i:
                width, height = i.size
                i.thumbnail((740, 740))
                i.save(UPLOAD_DIR / f"{content_hash}_resized", format=i.format)
        except Exception:
            logger.exception(
                f"Failed to created thumbnail for {f.filename}/{content_hash}"
            )
        else:
            has_thumbnail = True
            logger.info("Thumbnail generated")

    new_upload = models.Upload(
        content_type=f.content_type,
        content_hash=content_hash,
        has_thumbnail=has_thumbnail,
        blurhash=image_blurhash,
        width=width,
        height=height,
    )
    db.add(new_upload)
    db.commit()

    return new_upload


def upload_to_attachment(upload: models.Upload, filename: str) -> ap.RawObject:
    extra_attachment_fields = {}
    if upload.blurhash:
        extra_attachment_fields.update(
            {
                "blurhash": upload.blurhash,
                "height": upload.height,
                "width": upload.width,
            }
        )
    return {
        "type": "Document",
        "mediaType": upload.content_type,
        "name": filename,
        "url": BASE_URL + f"/attachments/{upload.content_hash}",
        **extra_attachment_fields,
    }
