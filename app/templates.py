import base64
from datetime import datetime
from datetime import timezone
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import bleach
import timeago  # type: ignore
from bs4 import BeautifulSoup  # type: ignore
from fastapi import Request
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.orm import Session
from starlette.templating import _TemplateResponse as TemplateResponse

from app import activitypub as ap
from app import models
from app.actor import LOCAL_ACTOR
from app.ap_object import Attachment
from app.ap_object import Object
from app.config import BASE_URL
from app.config import DEBUG
from app.config import VERSION
from app.config import generate_csrf_token
from app.config import session_serializer
from app.database import now
from app.media import proxied_media_url
from app.utils.highlight import HIGHLIGHT_CSS
from app.utils.highlight import highlight

_templates = Jinja2Templates(directory="app/templates")


def _filter_domain(text: str) -> str:
    hostname = urlparse(text).hostname
    if not hostname:
        raise ValueError(f"No hostname for {text}")
    return hostname


def _media_proxy_url(url: str | None) -> str:
    if not url:
        return "/static/nopic.png"

    if url.startswith(BASE_URL):
        return url

    encoded_url = base64.urlsafe_b64encode(url.encode()).decode()
    return f"/proxy/media/{encoded_url}"


def is_current_user_admin(request: Request) -> bool:
    is_admin = False
    session_cookie = request.cookies.get("session")
    if session_cookie:
        try:
            loaded_session = session_serializer.loads(
                session_cookie,
                max_age=3600 * 12,
            )
        except Exception:
            pass
        else:
            is_admin = loaded_session.get("is_logged_in")

    return is_admin


def render_template(
    db: Session,
    request: Request,
    template: str,
    template_args: dict[str, Any] = {},
) -> TemplateResponse:
    is_admin = False
    is_admin = is_current_user_admin(request)

    return _templates.TemplateResponse(
        template,
        {
            "request": request,
            "debug": DEBUG,
            "microblogpub_version": VERSION,
            "is_admin": is_admin,
            "csrf_token": generate_csrf_token() if is_admin else None,
            "highlight_css": HIGHLIGHT_CSS,
            "visibility_enum": ap.VisibilityEnum,
            "notifications_count": db.query(models.Notification)
            .filter(models.Notification.is_new.is_(True))
            .count()
            if is_admin
            else 0,
            "local_actor": LOCAL_ACTOR,
            "followers_count": db.query(models.Follower).count(),
            "following_count": db.query(models.Following).count(),
            **template_args,
        },
    )


# HTML/templates helper
ALLOWED_TAGS = [
    "a",
    "abbr",
    "acronym",
    "b",
    "br",
    "blockquote",
    "code",
    "pre",
    "em",
    "i",
    "li",
    "ol",
    "strong",
    "sup",
    "sub",
    "del",
    "ul",
    "span",
    "div",
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "th",
    "tr",
    "td",
    "thead",
    "tbody",
    "tfoot",
    "colgroup",
    "caption",
    "img",
]

ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "abbr": ["title"],
    "acronym": ["title"],
    "img": ["src", "alt", "title"],
}


@lru_cache(maxsize=256)
def _update_inline_imgs(content):
    soup = BeautifulSoup(content, "html5lib")
    imgs = soup.find_all("img")
    if not imgs:
        return content

    for img in imgs:
        if not img.attrs.get("src"):
            continue

        img.attrs["src"] = _media_proxy_url(img.attrs["src"])

    return soup.find("body").decode_contents()


def _clean_html(html: str, note: Object) -> str:
    try:
        return _replace_custom_emojis(
            bleach.clean(
                _update_inline_imgs(highlight(html)),
                tags=ALLOWED_TAGS,
                attributes=ALLOWED_ATTRIBUTES,
                strip=True,
            ),
            note,
        )
    except Exception:
        raise


def _timeago(original_dt: datetime) -> str:
    dt = original_dt
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return timeago.format(dt, now().replace(tzinfo=None))


def _has_media_type(attachment: Attachment, media_type_prefix: str) -> bool:
    return attachment.media_type.startswith(media_type_prefix)


def _format_date(dt: datetime) -> str:
    return dt.strftime("%b %d, %Y, %H:%M")


def _pluralize(count: int, singular: str = "", plural: str = "s") -> str:
    if count > 1:
        return plural
    else:
        return singular


def _replace_custom_emojis(content: str, note: Object) -> str:
    idx = {}
    for tag in note.tags:
        if tag.get("type") == "Emoji":
            try:
                idx[tag["name"]] = proxied_media_url(tag["icon"]["url"])
            except KeyError:
                logger.warning(f"Failed to parse custom emoji {tag=}")
                continue

    for emoji_name, emoji_url in idx.items():
        content = content.replace(
            emoji_name,
            f'<img class="custom-emoji" src="{emoji_url}" title="{emoji_name}" alt="{emoji_name}">',  # noqa: E501
        )

    return content


_templates.env.filters["domain"] = _filter_domain
_templates.env.filters["media_proxy_url"] = _media_proxy_url
_templates.env.filters["clean_html"] = _clean_html
_templates.env.filters["timeago"] = _timeago
_templates.env.filters["format_date"] = _format_date
_templates.env.filters["has_media_type"] = _has_media_type
_templates.env.filters["pluralize"] = _pluralize
