import datetime
from dataclasses import dataclass
from typing import Any
from typing import Optional

from loguru import logger

from app import media
from app.models import InboxObject
from app.models import Webmention
from app.utils.datetime import parse_isoformat
from app.utils.url import make_abs


@dataclass
class Face:
    ap_actor_id: str | None
    url: str
    name: str
    picture_url: str
    created_at: datetime.datetime

    @classmethod
    def from_inbox_object(cls, like: InboxObject) -> "Face":
        return cls(
            ap_actor_id=like.actor.ap_id,
            url=like.actor.url,  # type: ignore
            name=like.actor.handle,  # type: ignore
            picture_url=like.actor.resized_icon_url,
            created_at=like.created_at,  # type: ignore
        )

    @classmethod
    def from_webmention(cls, webmention: Webmention) -> Optional["Face"]:
        items = webmention.source_microformats.get("items", [])  # type: ignore
        for item in items:
            if item["type"][0] == "h-card":
                try:
                    return cls(
                        ap_actor_id=None,
                        url=(
                            item["properties"]["url"][0]
                            if item["properties"].get("url")
                            else webmention.source
                        ),
                        name=item["properties"]["name"][0],
                        picture_url=media.resized_media_url(
                            make_abs(
                                item["properties"]["photo"][0], webmention.source
                            ),  # type: ignore
                            50,
                        ),
                        created_at=webmention.created_at,  # type: ignore
                    )
                except Exception:
                    logger.exception(
                        f"Failed to build Face for webmention id={webmention.id}"
                    )
                    break
            elif item["type"][0] == "h-entry":
                author = item["properties"]["author"][0]
                try:
                    return cls(
                        ap_actor_id=None,
                        url=webmention.source,
                        name=author["properties"]["name"][0],
                        picture_url=media.resized_media_url(
                            make_abs(
                                author["properties"]["photo"][0], webmention.source
                            ),  # type: ignore
                            50,
                        ),
                        created_at=webmention.created_at,  # type: ignore
                    )
                except Exception:
                    logger.exception(
                        f"Failed to build Face for webmention id={webmention.id}"
                    )
                    break

        return None


def merge_faces(faces: list[Face]) -> list[Face]:
    return sorted(
        faces,
        key=lambda f: f.created_at,
        reverse=True,
    )[:10]


def _parse_face(webmention: Webmention, items: list[dict[str, Any]]) -> Face | None:
    for item in items:
        if item["type"][0] == "h-card":
            try:
                return Face(
                    ap_actor_id=None,
                    url=(
                        item["properties"]["url"][0]
                        if item["properties"].get("url")
                        else webmention.source
                    ),
                    name=item["properties"]["name"][0],
                    picture_url=media.resized_media_url(
                        make_abs(
                            item["properties"]["photo"][0], webmention.source
                        ),  # type: ignore
                        50,
                    ),
                    created_at=webmention.created_at,  # type: ignore
                )
            except Exception:
                logger.exception(
                    f"Failed to build Face for webmention id={webmention.id}"
                )
                break

    return None


@dataclass
class WebmentionReply:
    face: Face
    content: str
    url: str
    published_at: datetime.datetime
    in_reply_to: str
    webmention_id: int

    @classmethod
    def from_webmention(cls, webmention: Webmention) -> Optional["WebmentionReply"]:
        items = webmention.source_microformats.get("items", [])  # type: ignore
        for item in items:
            if item["type"][0] == "h-entry":
                try:
                    face = _parse_face(webmention, item["properties"].get("author", []))
                    if not face:
                        logger.info(
                            "Failed to build WebmentionReply/Face for "
                            f"webmention id={webmention.id}"
                        )
                        break
                    return cls(
                        face=face,
                        content=item["properties"]["content"][0]["html"],
                        url=item["properties"]["url"][0],
                        published_at=parse_isoformat(
                            item["properties"]["published"][0]
                        ).replace(tzinfo=None),
                        in_reply_to=webmention.target,  # type: ignore
                        webmention_id=webmention.id,  # type: ignore
                    )
                except Exception:
                    logger.exception(
                        f"Failed to build Face for webmention id={webmention.id}"
                    )
                    break

        return None
