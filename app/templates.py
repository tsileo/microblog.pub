import base64
from datetime import datetime
from datetime import timezone
from functools import lru_cache
from typing import Any
from typing import Callable
from urllib.parse import urlparse

import bleach
import emoji
import html2text
import humanize
from bs4 import BeautifulSoup  # type: ignore
from fastapi import Request
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import func
from sqlalchemy import select
from starlette.templating import _TemplateResponse as TemplateResponse

from app import activitypub as ap
from app import config
from app import models
from app.actor import LOCAL_ACTOR
from app.ap_object import Attachment
from app.ap_object import Object
from app.config import BASE_URL
from app.config import DEBUG
from app.config import VERSION
from app.config import generate_csrf_token
from app.config import session_serializer
from app.database import AsyncSession
from app.database import now
from app.media import proxied_media_url
from app.utils.highlight import HIGHLIGHT_CSS
from app.utils.highlight import highlight

_templates = Jinja2Templates(directory="app/templates")


H2T = html2text.HTML2Text()
H2T.ignore_links = True
H2T.ignore_images = True


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


async def render_template(
    db_session: AsyncSession,
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
            "notifications_count": await db_session.scalar(
                select(func.count(models.Notification.id)).where(
                    models.Notification.is_new.is_(True)
                )
            )
            if is_admin
            else 0,
            "local_actor": LOCAL_ACTOR,
            "followers_count": await db_session.scalar(
                select(func.count(models.Follower.id))
            ),
            "following_count": await db_session.scalar(
                select(func.count(models.Following.id))
            ),
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
    "div",
    "span",
]

ALLOWED_CSS_CLASSES = [
    "highlight",
    "codehilite",
    "hll",
    "c",
    "err",
    "g",
    "k",
    "l",
    "n",
    "o",
    "x",
    "p",
    "ch",
    "cm",
    "cp",
    "cpf",
    "c1",
    "cs",
    "gd",
    "ge",
    "gr",
    "gh",
    "gi",
    "go",
    "gp",
    "gs",
    "gu",
    "gt",
    "kc",
    "kd",
    "kn",
    "kp",
    "kr",
    "kt",
    "ld",
    "m",
    "s",
    "na",
    "nb",
    "nc",
    "no",
    "nd",
    "ni",
    "ne",
    "nf",
    "nl",
    "nn",
    "nx",
    "py",
    "nt",
    "nv",
    "ow",
    "w",
    "mb",
    "mf",
    "mh",
    "mi",
    "mo",
    "sa",
    "sb",
    "sc",
    "dl",
    "sd",
    "s2",
    "se",
    "sh",
    "si",
    "sx",
    "sr",
    "s1",
    "ss",
    "bp",
    "fm",
    "vc",
    "vg",
    "vi",
    "vm",
    "il",
]


def _allow_class(_tag: str, name: str, value: str) -> bool:
    return name == "class" and value in ALLOWED_CSS_CLASSES


ALLOWED_ATTRIBUTES: dict[str, list[str] | Callable[[str, str, str], bool]] = {
    "a": ["href", "title"],
    "abbr": ["title"],
    "acronym": ["title"],
    "img": ["src", "alt", "title"],
    "div": _allow_class,
    "span": _allow_class,
    "code": _allow_class,
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
        return _emojify(
            _replace_custom_emojis(
                bleach.clean(
                    _update_inline_imgs(highlight(html)),
                    tags=ALLOWED_TAGS,
                    attributes=ALLOWED_ATTRIBUTES,
                    strip=True,
                ),
                note,
            ),
            is_local=note.ap_id.startswith(BASE_URL),
        )
    except Exception:
        raise


def _timeago(original_dt: datetime) -> str:
    dt = original_dt
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return humanize.naturaltime(dt, when=now().replace(tzinfo=None))


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


def _html2text(content: str) -> str:
    return H2T.handle(content)


def _replace_emoji(u: str, _) -> str:
    filename = hex(ord(u))[2:]
    return config.EMOJI_TPL.format(filename=filename, raw=u)


def _emojify(text: str, is_local: bool) -> str:
    if not is_local:
        return text

    return emoji.replace_emoji(
        text,
        replace=_replace_emoji,
    )


_templates.env.filters["domain"] = _filter_domain
_templates.env.filters["media_proxy_url"] = _media_proxy_url
_templates.env.filters["clean_html"] = _clean_html
_templates.env.filters["timeago"] = _timeago
_templates.env.filters["format_date"] = _format_date
_templates.env.filters["has_media_type"] = _has_media_type
_templates.env.filters["html2text"] = _html2text
_templates.env.filters["emojify"] = _emojify
_templates.env.filters["pluralize"] = _pluralize
