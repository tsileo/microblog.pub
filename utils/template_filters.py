import logging
import urllib
from datetime import datetime
from datetime import timezone
from urllib.parse import urlparse

import bleach
import emoji_unicode
import flask
import html2text
import timeago
from cachetools import LRUCache
from little_boxes import activitypub as ap
from little_boxes.activitypub import _to_list
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import ActivityNotFoundError

from config import BASE_URL
from config import EMOJI_TPL
from config import ID
from config import MEDIA_CACHE
from core.activitypub import _answer_key
from utils import parse_datetime
from utils.highlight import highlight
from utils.media import Kind
from utils.media import _is_img

_logger = logging.getLogger(__name__)

H2T = html2text.HTML2Text()
H2T.ignore_links = True
H2T.ignore_images = True


filters = flask.Blueprint("filters", __name__)


@filters.app_template_filter()
def get_visibility(meta):
    if "object_visibility" in meta and meta["object_visibility"]:
        return meta["object_visibility"]
    return meta.get("visibility")


@filters.app_template_filter()
def visibility(v: str) -> str:
    try:
        return ap.Visibility[v].value.lower()
    except Exception:
        return v


@filters.app_template_filter()
def visibility_is_public(v: str) -> bool:
    return v in [ap.Visibility.PUBLIC.name, ap.Visibility.UNLISTED.name]


@filters.app_template_filter()
def code_highlight(content):
    return highlight(content)


@filters.app_template_filter()
def emojify(text):
    return emoji_unicode.replace(
        text, lambda e: EMOJI_TPL.format(filename=e.code_points, raw=e.unicode)
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
]


@filters.app_template_filter()
def replace_custom_emojis(content, note):
    idx = {}
    for tag in note.get("tag", []):
        if tag.get("type") == "Emoji":
            # try:
            idx[tag["name"]] = _get_file_url(tag["icon"]["url"], 25, Kind.EMOJI)

    for emoji_name, emoji_url in idx.items():
        content = content.replace(
            emoji_name,
            f'<img class="custom-emoji" src="{emoji_url}" title="{emoji_name}" alt="{emoji_name}">',
        )

    return content


def clean_html(html):
    try:
        return bleach.clean(html, tags=ALLOWED_TAGS, strip=True)
    except Exception:
        return "failed to clean HTML"


@filters.app_template_filter()
def gtone(n):
    return n > 1


@filters.app_template_filter()
def gtnow(dtstr):
    return ap.format_datetime(datetime.now(timezone.utc)) > dtstr


@filters.app_template_filter()
def clean(html):
    out = clean_html(html)
    return emoji_unicode.replace(
        out, lambda e: EMOJI_TPL.format(filename=e.code_points, raw=e.unicode)
    )


@filters.app_template_filter()
def permalink_id(val):
    return str(hash(val))


@filters.app_template_filter()
def quote_plus(t):
    return urllib.parse.quote_plus(t)


@filters.app_template_filter()
def is_from_outbox(t):
    return t.startswith(ID)


@filters.app_template_filter()
def html2plaintext(body):
    return H2T.handle(body)


@filters.app_template_filter()
def domain(url):
    return urlparse(url).netloc


@filters.app_template_filter()
def format_time(val):
    if val:
        dt = parse_datetime(val)
        return datetime.strftime(dt, "%B %d, %Y, %H:%M %p")
    return val


@filters.app_template_filter()
def format_ts(val):
    return datetime.fromtimestamp(val).strftime("%B %d, %Y, %H:%M %p")


@filters.app_template_filter()
def gt_ts(val):
    return datetime.now() > datetime.fromtimestamp(val)


@filters.app_template_filter()
def format_timeago(val):
    if val:
        dt = parse_datetime(val)
        return timeago.format(dt.astimezone(timezone.utc), datetime.now(timezone.utc))
    return val


@filters.app_template_filter()
def url_or_id(d):
    if isinstance(d, dict):
        if "url" in d:
            return d["url"]
        else:
            return d["id"]
    return ""


@filters.app_template_filter()
def get_url(u):
    if isinstance(u, list):
        for l in u:
            if l.get("mimeType") == "text/html":
                u = l
    if isinstance(u, dict):
        return u["href"]
    elif isinstance(u, str):
        return u
    else:
        return u


@filters.app_template_filter()
def get_actor(url):
    if not url:
        return None
    if isinstance(url, list):
        url = url[0]
    if isinstance(url, dict):
        url = url.get("id")
    try:
        return ap.get_backend().fetch_iri(url)
    except (ActivityNotFoundError, ActivityGoneError):
        return f"Deleted<{url}>"
    except Exception as exc:
        return f"Error<{url}/{exc!r}>"


@filters.app_template_filter()
def poll_answer_key(choice: str) -> str:
    return _answer_key(choice)


@filters.app_template_filter()
def get_answer_count(choice, obj, meta):
    count_from_meta = meta.get("question_answers", {}).get(_answer_key(choice), 0)
    if count_from_meta:
        return count_from_meta
    for option in obj.get("oneOf", obj.get("anyOf", [])):
        if option.get("name") == choice:
            return option.get("replies", {}).get("totalItems", 0)

    _logger.warning(f"invalid poll data {choice} {obj} {meta}")
    return 0


@filters.app_template_filter()
def get_total_answers_count(obj, meta):
    cached = meta.get("question_replies", 0)
    if cached:
        return cached
    cnt = 0
    for choice in obj.get("anyOf", obj.get("oneOf", [])):
        cnt += choice.get("replies", {}).get("totalItems", 0)
    return cnt


_FILE_URL_CACHE = LRUCache(4096)


def _get_file_url(url, size, kind) -> str:
    k = (url, size, kind)
    cached = _FILE_URL_CACHE.get(k)
    if cached:
        return cached

    doc = MEDIA_CACHE.get_file(*k)
    if doc:
        out = f"/media/{str(doc._id)}"
        _FILE_URL_CACHE[k] = out
        return out

    _logger.error(f"cache not available for {url}/{size}/{kind}")
    if url.startswith(BASE_URL):
        return url

    p = urlparse(url)
    return f"/p/{p.scheme}" + p._replace(scheme="").geturl()[1:]


@filters.app_template_filter()
def get_actor_icon_url(url, size):
    return _get_file_url(url, size, Kind.ACTOR_ICON)


@filters.app_template_filter()
def get_attachment_url(url, size):
    return _get_file_url(url, size, Kind.ATTACHMENT)


@filters.app_template_filter()
def get_video_url(url):
    if isinstance(url, list):
        for link in url:
            if link.get("mimeType", "").startswith("video/"):
                return _get_file_url(link.get("href"), None, Kind.ATTACHMENT)
    else:
        return _get_file_url(url, None, Kind.ATTACHMENT)


@filters.app_template_filter()
def get_og_image_url(url, size=100):
    try:
        return _get_file_url(url, size, Kind.OG_IMAGE)
    except Exception:
        return ""


@filters.app_template_filter()
def remove_mongo_id(dat):
    if isinstance(dat, list):
        return [remove_mongo_id(item) for item in dat]
    if "_id" in dat:
        dat["_id"] = str(dat["_id"])
    for k, v in dat.items():
        if isinstance(v, dict):
            dat[k] = remove_mongo_id(dat[k])
    return dat


@filters.app_template_filter()
def get_video_link(data):
    if isinstance(data, list):
        for link in data:
            if link.get("mimeType", "").startswith("video/"):
                return link.get("href")
    elif isinstance(data, str):
        return data
    return None


@filters.app_template_filter()
def has_type(doc, _types):
    for _type in _to_list(_types):
        if _type in _to_list(doc["type"]):
            return True
    return False


@filters.app_template_filter()
def has_actor_type(doc):
    # FIXME(tsileo): skipping the last one "Question", cause Mastodon sends question restuls as an update coming from
    # the question... Does Pleroma do that too?
    for t in ap.ACTOR_TYPES[:-1]:
        if has_type(doc, t.value):
            return True
    return False


@filters.app_template_filter()
def not_only_imgs(attachment):
    for a in attachment:
        if isinstance(a, dict) and not _is_img(a["url"]):
            return True
        if isinstance(a, str) and not _is_img(a):
            return True
    return False


@filters.app_template_filter()
def is_img(filename):
    return _is_img(filename)
