from bs4 import BeautifulSoup  # type: ignore
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.utils import microformats
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

    # TODO: get outbox via ap_id (URL is the same as ap_id)
    maybe_data_and_html = await microformats.fetch_and_parse(source)
    if not maybe_data_and_html:
        logger.info("failed to fetch source")
        raise HTTPException(status_code=400, detail="failed to fetch source")

    data, html = maybe_data_and_html

    if not is_source_containing_target(html, target):
        logger.warning("target not found in source")
        raise HTTPException(status_code=400, detail="target not found in source")

    logger.info(f"{data=}")

    return JSONResponse(content={}, status_code=200)
