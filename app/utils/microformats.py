from typing import Any

import httpx
import mf2py  # type: ignore
from loguru import logger

from app import config


class URLNotFoundOrGone(Exception):
    pass


async def fetch_and_parse(url: str) -> tuple[dict[str, Any], str]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "User-Agent": config.USER_AGENT,
            },
            follow_redirects=True,
        )
        if resp.status_code in [404, 410]:
            raise URLNotFoundOrGone

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(
                f"Failed to parse microformats for {url}: " f"got {resp.status_code}"
            )
            raise

    return mf2py.parse(doc=resp.text), resp.text
