from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup  # type: ignore
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import func
from sqlalchemy import select

from app import models
from app.boxes import _get_outbox_announces_count
from app.boxes import _get_outbox_likes_count
from app.boxes import _get_outbox_replies_count
from app.boxes import get_outbox_object_by_ap_id
from app.boxes import get_outbox_object_by_slug_and_short_id
from app.database import AsyncSession
from app.database import get_db_session
from app.utils import microformats
from app.utils.facepile import Face
from app.utils.facepile import WebmentionReply
from app.utils.url import check_url
from app.utils.url import is_url_valid

router = APIRouter()


def is_source_containing_target(source_html: str, target_url: str) -> bool:
    soup = BeautifulSoup(source_html, "html5lib")
    for link in soup.find_all("a"):
        h = link.get("href")
        if not is_url_valid(h):
            continue

        if h == target_url:
            return True

    return False


@router.post("/webmentions")
async def webmention_endpoint(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    form_data = await request.form()
    try:
        source = form_data["source"]
        target = form_data["target"]

        if source == target:
            raise ValueError("source URL is the same as target")

        check_url(source)
        check_url(target)
        parsed_target_url = urlparse(target)
    except Exception:
        logger.exception("Invalid webmention request")
        raise HTTPException(status_code=400, detail="Invalid payload")

    logger.info(f"Received webmention {source=} {target=}")

    existing_webmention_in_db = (
        await db_session.execute(
            select(models.Webmention).where(
                models.Webmention.source == source,
                models.Webmention.target == target,
            )
        )
    ).scalar_one_or_none()
    if existing_webmention_in_db:
        logger.info("Found existing Webmention, will try to update or delete")

    mentioned_object = await get_outbox_object_by_ap_id(db_session, target)

    if not mentioned_object and parsed_target_url.path.startswith("/articles/"):
        try:
            _, _, short_id, slug = parsed_target_url.path.split("/")
            mentioned_object = await get_outbox_object_by_slug_and_short_id(
                db_session, slug, short_id
            )
        except Exception:
            logger.exception(f"Failed to match {target}")

    if not mentioned_object:
        logger.info(f"Invalid target {target=}")

        if existing_webmention_in_db:
            logger.info("Deleting existing Webmention")
            existing_webmention_in_db.is_deleted = True
            await db_session.commit()
        raise HTTPException(status_code=400, detail="Invalid target")

    is_webmention_deleted = False
    try:
        data_and_html = await microformats.fetch_and_parse(source)
    except microformats.URLNotFoundOrGone:
        is_webmention_deleted = True
    except httpx.HTTPError:
        raise HTTPException(status_code=500, detail=f"Fetch to process {source}")

    data, html = data_and_html
    is_target_found_in_source = is_source_containing_target(html, target)

    data, html = data_and_html
    if is_webmention_deleted or not is_target_found_in_source:
        logger.warning(f"target {target=} not found in source")
        if existing_webmention_in_db:
            logger.info("Deleting existing Webmention")
            existing_webmention_in_db.is_deleted = True
            await db_session.flush()

            # Revert side effects
            await _handle_webmention_side_effects(
                db_session, existing_webmention_in_db, mentioned_object
            )

            notif = models.Notification(
                notification_type=models.NotificationType.DELETED_WEBMENTION,
                outbox_object_id=mentioned_object.id,
                webmention_id=existing_webmention_in_db.id,
            )
            db_session.add(notif)

            await db_session.commit()

        if not is_target_found_in_source:
            raise HTTPException(
                status_code=400,
                detail="target not found in source",
            )
        else:
            return JSONResponse(content={}, status_code=200)

    webmention_type = models.WebmentionType.UNKNOWN
    webmention: models.Webmention
    if existing_webmention_in_db:
        # Undelete if needed
        existing_webmention_in_db.is_deleted = False
        existing_webmention_in_db.source_microformats = data
        await db_session.flush()
        webmention = existing_webmention_in_db

        notif = models.Notification(
            notification_type=models.NotificationType.UPDATED_WEBMENTION,
            outbox_object_id=mentioned_object.id,
            webmention_id=existing_webmention_in_db.id,
        )
        db_session.add(notif)
    else:
        new_webmention = models.Webmention(
            source=source,
            target=target,
            source_microformats=data,
            outbox_object_id=mentioned_object.id,
            webmention_type=webmention_type,
        )
        db_session.add(new_webmention)
        await db_session.flush()
        webmention = new_webmention

        notif = models.Notification(
            notification_type=models.NotificationType.NEW_WEBMENTION,
            outbox_object_id=mentioned_object.id,
            webmention_id=new_webmention.id,
        )
        db_session.add(notif)

    # Determine the webmention type
    for item in data.get("items", []):
        if target in item.get("properties", {}).get(
            "in-reply-to", []
        ) and WebmentionReply.from_webmention(webmention):
            webmention_type = models.WebmentionType.REPLY
            break
        elif target in item.get("properties", {}).get(
            "like-of", []
        ) and Face.from_webmention(webmention):
            webmention_type = models.WebmentionType.LIKE
            break
        elif target in item.get("properties", {}).get(
            "repost-of", []
        ) and Face.from_webmention(webmention):
            webmention_type = models.WebmentionType.REPOST
            break

    if webmention_type != models.WebmentionType.UNKNOWN:
        webmention.webmention_type = webmention_type
        await db_session.flush()

    # Handle side effect
    await _handle_webmention_side_effects(db_session, webmention, mentioned_object)
    await db_session.commit()

    return JSONResponse(content={}, status_code=200)


async def _handle_webmention_side_effects(
    db_session: AsyncSession,
    webmention: models.Webmention,
    mentioned_object: models.OutboxObject,
) -> None:
    if webmention.webmention_type == models.WebmentionType.UNKNOWN:
        # TODO: recount everything
        mentioned_object.webmentions_count = await db_session.scalar(
            select(func.count(models.Webmention.id)).where(
                models.Webmention.is_deleted.is_(False),
                models.Webmention.outbox_object_id == mentioned_object.id,
                models.Webmention.webmention_type == models.WebmentionType.UNKNOWN,
            )
        )
    elif webmention.webmention_type == models.WebmentionType.LIKE:
        mentioned_object.likes_count = await _get_outbox_likes_count(
            db_session, mentioned_object
        )
    elif webmention.webmention_type == models.WebmentionType.REPOST:
        mentioned_object.announces_count = await _get_outbox_announces_count(
            db_session, mentioned_object
        )
    elif webmention.webmention_type == models.WebmentionType.REPLY:
        mentioned_object.replies_count = await _get_outbox_replies_count(
            db_session, mentioned_object
        )
    else:
        raise ValueError(f"Unhandled {webmention.webmention_type} webmention")
