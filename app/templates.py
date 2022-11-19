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
from dateutil.parser import parse
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
from app.config import CUSTOM_FOOTER
from app.config import DEBUG
from app.config import VERSION
from app.config import generate_csrf_token
from app.config import session_serializer
from app.database import AsyncSession
from app.media import proxied_media_url
from app.utils import privacy_replace
from app.utils.datetime import now
from app.utils.highlight import HIGHLIGHT_CSS
from app.utils.highlight import highlight

_templates = Jinja2Templates(
    directory=["data/templates", "app/templates"],  # type: ignore  # bad typing
    trim_blocks=True,
    lstrip_blocks=True,
)


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
        return BASE_URL + "/static/nopic.png"
    return proxied_media_url(url)


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
    template_args: dict[str, Any] | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> TemplateResponse:
    if template_args is None:
        template_args = {}

    is_admin = False
    is_admin = is_current_user_admin(request)

    return _templates.TemplateResponse(
        template,
        {
            "request": request,
            "debug": DEBUG,
            "microblogpub_version": VERSION,
            "is_admin": is_admin,
            "csrf_token": generate_csrf_token(),
            "highlight_css": HIGHLIGHT_CSS,
            "visibility_enum": ap.VisibilityEnum,
            "notifications_count": await db_session.scalar(
                select(func.count(models.Notification.id)).where(
                    models.Notification.is_new.is_(True)
                )
            )
            if is_admin
            else 0,
            "articles_count": await db_session.scalar(
                select(func.count(models.OutboxObject.id)).where(
                    models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
                    models.OutboxObject.is_deleted.is_(False),
                    models.OutboxObject.is_hidden_from_homepage.is_(False),
                    models.OutboxObject.ap_type == "Article",
                )
            ),
            "local_actor": LOCAL_ACTOR,
            "followers_count": await db_session.scalar(
                select(func.count(models.Follower.id))
            ),
            "following_count": await db_session.scalar(
                select(func.count(models.Following.id))
            ),
            "actor_types": ap.ACTOR_TYPES,
            "custom_footer": CUSTOM_FOOTER,
            **template_args,
        },
        status_code=status_code,
        headers=headers,
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
    # microformats
    "h-card",
    "u-url",
    "mention",
    # code highlighting
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


def _allow_img_attrs(_tag: str, name: str, value: str) -> bool:
    if name in ["src", "alt", "title"]:
        return True
    if name == "class" and value == "inline-img":
        return True

    return False


ALLOWED_ATTRIBUTES: dict[str, list[str] | Callable[[str, str, str], bool]] = {
    "a": ["href", "title"],
    "abbr": ["title"],
    "acronym": ["title"],
    "img": _allow_img_attrs,
    "div": _allow_class,
    "span": _allow_class,
    "code": _allow_class,
}


def _allow_all_attributes(tag: Any, name: Any, value: Any) -> bool:
    return True


@lru_cache(maxsize=256)
def _update_inline_imgs(content):
    soup = BeautifulSoup(content, "html5lib")
    imgs = soup.find_all("img")
    if not imgs:
        return content

    for img in imgs:
        if not img.attrs.get("src"):
            continue

        img.attrs["src"] = _media_proxy_url(img.attrs["src"]) + "/740"
        img["class"] = "inline-img"

    return soup.find("body").decode_contents()


def _clean_html(html: str, note: Object) -> str:
    if html is None:
        logger.error(f"{html=} for {note.ap_id}/{note.ap_object}")
        return ""
    try:
        return _emojify(
            _replace_custom_emojis(
                bleach.clean(
                    privacy_replace.replace_content(
                        _update_inline_imgs(highlight(html))
                    ),
                    tags=ALLOWED_TAGS,
                    attributes=(
                        _allow_all_attributes
                        if note.ap_id.startswith(config.ID)
                        else ALLOWED_ATTRIBUTES
                    ),
                    strip=True,
                ),
                note,
            ),
            is_local=note.ap_id.startswith(BASE_URL),
        )
    except Exception:
        raise


def _clean_html_wm(html: str) -> str:
    return bleach.clean(
        html,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )


def _timeago(original_dt: datetime) -> str:
    dt = original_dt
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return humanize.naturaltime(dt, when=now().replace(tzinfo=None))


def _has_media_type(attachment: Attachment, media_type_prefix: str) -> bool:
    if attachment.media_type:
        return attachment.media_type.startswith(media_type_prefix)
    return False


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
    filename = "-".join(hex(ord(c))[2:] for c in u)
    return config.EMOJI_TPL.format(base_url=BASE_URL, filename=filename, raw=u)


def _emojify(text: str, is_local: bool) -> str:
    if not is_local:
        return text

    return emoji.replace_emoji(
        text,
        replace=_replace_emoji,
    )


def _parse_datetime(dt: str) -> datetime:
    return parse(dt)


def _poll_item_pct(item: ap.RawObject, voters_count: int) -> int:
    if voters_count == 0:
        return 0

    return int(item["replies"]["totalItems"] * 100 / voters_count)


_templates.env.filters["domain"] = _filter_domain
_templates.env.filters["media_proxy_url"] = _media_proxy_url
_templates.env.filters["clean_html"] = _clean_html
_templates.env.filters["clean_html_wm"] = _clean_html_wm
_templates.env.filters["timeago"] = _timeago
_templates.env.filters["format_date"] = _format_date
_templates.env.filters["has_media_type"] = _has_media_type
_templates.env.filters["html2text"] = _html2text
_templates.env.filters["emojify"] = _emojify
_templates.env.filters["pluralize"] = _pluralize
_templates.env.filters["parse_datetime"] = _parse_datetime
_templates.env.filters["poll_item_pct"] = _poll_item_pct
_templates.env.filters["privacy_replace_url"] = privacy_replace.replace_url
_templates.env.globals["JS_HASH"] = config.JS_HASH
_templates.env.globals["CSS_HASH"] = config.CSS_HASH
_templates.env.globals["BASE_URL"] = config.BASE_URL
_templates.env.globals["HIDES_FOLLOWERS"] = config.HIDES_FOLLOWERS
_templates.env.globals["HIDES_FOLLOWING"] = config.HIDES_FOLLOWING
_templates.env.globals["NAVBAR_ITEMS"] = config.NavBarItems
_templates.env.globals["ICON_URL"] = config.CONFIG.icon_url
