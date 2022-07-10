from dataclasses import dataclass
from typing import Any

import httpx
import mf2py  # type: ignore
from loguru import logger

from app import config
from app.utils.url import make_abs


@dataclass
class IndieAuthClient:
    logo: str | None
    name: str
    url: str


def _get_prop(props: dict[str, Any], name: str, default=None) -> Any:
    if name in props:
        items = props.get(name)
        if isinstance(items, list):
            return items[0]
        return items
    return default


async def get_client_id_data(url: str) -> IndieAuthClient | None:
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

    data = mf2py.parse(doc=resp.text)
    for item in data["items"]:
        if "h-x-app" in item["type"] or "h-app" in item["type"]:
            props = item.get("properties", {})
            print(props)
            logo = _get_prop(props, "logo")
            return IndieAuthClient(
                logo=make_abs(logo, url) if logo else None,
                name=_get_prop(props, "name"),
                url=_get_prop(props, "url", url),
            )

    return IndieAuthClient(
        logo=None,
        name=url,
        url=url,
    )
