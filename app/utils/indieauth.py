from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.utils import microformats
from app.utils.url import make_abs


@dataclass
class IndieAuthClient:
    logo: str | None
    name: str
    url: str | None


def _get_prop(props: dict[str, Any], name: str, default=None) -> Any:
    if name in props:
        items = props.get(name)
        if isinstance(items, list):
            return items[0]
        return items
    return default


async def get_client_id_data(url: str) -> IndieAuthClient | None:
    # Don't fetch localhost URL
    if urlparse(url).hostname == "localhost":
        return IndieAuthClient(
            logo=None,
            name=url,
            url=url,
        )

    maybe_data_and_html = await microformats.fetch_and_parse(url)
    if maybe_data_and_html is not None:
        data: dict[str, Any] = maybe_data_and_html[0]

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
