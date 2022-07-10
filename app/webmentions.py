from dataclasses import asdict
from dataclasses import dataclass
from typing import Any
from typing import Optional

from bs4 import BeautifulSoup  # type: ignore
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.boxes import get_outbox_object_by_ap_id
from app.database import AsyncSession
from app.database import get_db_session
from app.database import now
from app.utils import microformats
from app.utils.url import check_url
from app.utils.url import is_url_valid
from app.utils.url import make_abs

router = APIRouter()


@dataclass
class Webmention:
    actor_icon_url: str
    actor_name: str
    url: str
    received_at: str

    @classmethod
    def from_microformats(
        cls, items: list[dict[str, Any]], url: str
    ) -> Optional["Webmention"]:
        for item in items:
            if item["type"][0] == "h-card":
                return cls(
                    actor_icon_url=make_abs(
                        item["properties"]["photo"][0], url
                    ),  # type: ignore
                    actor_name=item["properties"]["name"][0],
                    url=url,
                    received_at=now().isoformat(),
                )
            if item["type"][0] == "h-entry":
                author = item["properties"]["author"][0]
                return cls(
                    actor_icon_url=make_abs(
                        author["properties"]["photo"][0], url
                    ),  # type: ignore
                    actor_name=author["properties"]["name"][0],
                    url=url,
                    received_at=now().isoformat(),
                )

        return None


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
    except Exception:
        logger.exception("Invalid webmention request")
        raise HTTPException(status_code=400, detail="Invalid payload")

    logger.info(f"Received webmention {source=} {target=}")

    mentioned_object = await get_outbox_object_by_ap_id(db_session, target)
    if not mentioned_object:
        logger.info(f"Invalid target {target=}")
        raise HTTPException(status_code=400, detail="Invalid target")

    maybe_data_and_html = await microformats.fetch_and_parse(source)
    if not maybe_data_and_html:
        logger.info("failed to fetch source")
        raise HTTPException(status_code=400, detail="failed to fetch source")

    data, html = maybe_data_and_html

    if not is_source_containing_target(html, target):
        logger.warning("target not found in source")
        raise HTTPException(status_code=400, detail="target not found in source")

    try:
        webmention = Webmention.from_microformats(data["items"], source)
        if not webmention:
            raise ValueError("Failed to fetch target data")
    except Exception:
        logger.warning("Failed build Webmention for {source=} with {data=}")
        return JSONResponse(content={}, status_code=200)

    logger.info(f"{webmention=}")

    if mentioned_object.webmentions is None:
        mentioned_object.webmentions = [asdict(webmention)]
    else:
        mentioned_object.webmentions = [asdict(webmention)] + [
            wm  # type: ignore
            for wm in mentioned_object.webmentions  # type: ignore
            if wm["url"] != source  # type: ignore
        ]

    await db_session.commit()

    return JSONResponse(content={}, status_code=200)
