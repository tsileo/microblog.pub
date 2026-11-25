from dataclasses import dataclass
from dataclasses import field
from typing import Any
from urllib.parse import urlparse

from app.utils import microformats
from app.utils.url import make_abs


@dataclass
class IndieAuthClient:
    logo: str | None
    name: str
    url: str | None
    redirect_uris: list[str] = field(default_factory=list)


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

        # Redirect URLs published by the client (`<link rel="redirect_uri">`)
        redirect_uris = [
            abs_uri
            for uri in data.get("rels", {}).get("redirect_uri", [])
            if (abs_uri := make_abs(uri, url))
        ]

        for item in data["items"]:
            if "h-x-app" in item["type"] or "h-app" in item["type"]:
                props = item.get("properties", {})
                logo = _get_prop(props, "logo")
                return IndieAuthClient(
                    logo=make_abs(logo, url) if logo else None,
                    name=_get_prop(props, "name"),
                    url=_get_prop(props, "url", url),
                    redirect_uris=redirect_uris,
                )

        return IndieAuthClient(
            logo=None,
            name=url,
            url=url,
            redirect_uris=redirect_uris,
        )

    return IndieAuthClient(
        logo=None,
        name=url,
        url=url,
    )


def is_redirect_uri_allowed(
    client_id: str,
    redirect_uri: str,
    published_redirect_uris: list[str],
) -> bool:
    """IndieAuth: the redirect URI must be on the same origin as the client ID,
    or be published by the client."""
    if redirect_uri in published_redirect_uris:
        return True

    client = urlparse(client_id)
    redirect = urlparse(redirect_uri)
    return bool(client.netloc) and (client.scheme, client.netloc) == (
        redirect.scheme,
        redirect.netloc,
    )
