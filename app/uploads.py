import hashlib
from shutil import COPY_BUFSIZE  # type: ignore

import blurhash  # type: ignore
from fastapi import UploadFile
from loguru import logger
from PIL import Image
from PIL import ImageOps
from sqlalchemy import select

from app import activitypub as ap
from app import models
from app.config import BASE_URL
from app.config import ROOT_DIR
from app.database import AsyncSession

UPLOAD_DIR = ROOT_DIR / "data" / "uploads"


async def save_upload(db_session: AsyncSession, f: UploadFile) -> models.Upload:
    # Compute the hash
    h = hashlib.blake2b(digest_size=32)
    while True:
        buf = f.file.read(COPY_BUFSIZE)
        if not buf:
            break
        h.update(buf)

    content_hash = h.hexdigest()
    f.file.seek(0)

    existing_upload = (
        await db_session.execute(
            select(models.Upload).where(models.Upload.content_hash == content_hash)
        )
    ).scalar_one_or_none()
    if existing_upload:
        logger.info(f"Upload with {content_hash=} already exists")
        return existing_upload

    logger.info(f"Creating new Upload with {content_hash=}")
    dest_filename = UPLOAD_DIR / content_hash

    has_thumbnail = False
    image_blurhash = None
    width = None
    height = None

    if f.content_type.startswith("image") and not f.content_type == "image/gif":
        with Image.open(f.file) as _original_image:
            # Fix image orientation (as we will remove the info from the EXIF
            # metadata)
            original_image = ImageOps.exif_transpose(_original_image)

            # Re-creating the image drop the EXIF metadata
            destination_image = Image.new(
                original_image.mode,
                original_image.size,
            )
            destination_image.putdata(original_image.getdata())
            destination_image.save(
                dest_filename,
                format=_original_image.format,  # type: ignore
            )

            with open(dest_filename, "rb") as dest_f:
                image_blurhash = blurhash.encode(dest_f, x_components=4, y_components=3)

            try:
                width, height = destination_image.size
                destination_image.thumbnail((740, 740))
                destination_image.save(
                    UPLOAD_DIR / f"{content_hash}_resized",
                    format="webp",
                )
            except Exception:
                logger.exception(
                    f"Failed to created thumbnail for {f.filename}/{content_hash}"
                )
            else:
                has_thumbnail = True
                logger.info("Thumbnail generated")
    else:
        with open(dest_filename, "wb") as dest:
            while True:
                buf = f.file.read(COPY_BUFSIZE)
                if not buf:
                    break
                dest.write(buf)

    new_upload = models.Upload(
        content_type=f.content_type,
        content_hash=content_hash,
        has_thumbnail=has_thumbnail,
        blurhash=image_blurhash,
        width=width,
        height=height,
    )
    db_session.add(new_upload)
    await db_session.commit()

    return new_upload


def upload_to_attachment(
    upload: models.Upload,
    filename: str,
    alt_text: str | None,
) -> ap.RawObject:
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
        "name": alt_text or filename,
        "url": BASE_URL + f"/attachments/{upload.content_hash}/{filename}",
        **extra_attachment_fields,
    }
