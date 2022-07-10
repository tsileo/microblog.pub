from typing import Any

import httpx
import mf2py  # type: ignore
from loguru import logger

from app import config


async def fetch_and_parse(url: str) -> tuple[dict[str, Any], str] | None:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": config.USER_AGENT,
                },
                follow_redirects=True,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.HTTPStatusError):
            logger.exception(f"Failed to discover webmention endpoint for {url}")
            return None

    return mf2py.parse(doc=resp.text), resp.text
