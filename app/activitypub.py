import enum
import json
import mimetypes
from typing import TYPE_CHECKING
from typing import Any

import httpx
from loguru import logger

from app import config
from app.config import ALSO_KNOWN_AS
from app.config import AP_CONTENT_TYPE  # noqa: F401
from app.config import MOVED_TO
from app.httpsig import auth
from app.key import get_pubkey_as_pem
from app.source import dedup_tags
from app.source import hashtagify
from app.utils.url import check_url

if TYPE_CHECKING:
    from app.actor import Actor

RawObject = dict[str, Any]
AS_CTX = "https://www.w3.org/ns/activitystreams"
AS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"

ACTOR_TYPES = ["Application", "Group", "Organization", "Person", "Service"]

AS_EXTENDED_CTX = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    {
        # AS ext
        "Hashtag": "as:Hashtag",
        "sensitive": "as:sensitive",
        "manuallyApprovesFollowers": "as:manuallyApprovesFollowers",
        "alsoKnownAs": {"@id": "as:alsoKnownAs", "@type": "@id"},
        "movedTo": {"@id": "as:movedTo", "@type": "@id"},
        # toot
        "toot": "http://joinmastodon.org/ns#",
        "featured": {"@id": "toot:featured", "@type": "@id"},
        "Emoji": "toot:Emoji",
        "blurhash": "toot:blurhash",
        "votersCount": "toot:votersCount",
        # schema
        "schema": "http://schema.org#",
        "PropertyValue": "schema:PropertyValue",
        "value": "schema:value",
        # ostatus
        "ostatus": "http://ostatus.org#",
        "conversation": "ostatus:conversation",
    },
]


class FetchError(Exception):
    def __init__(self, url: str, resp: httpx.Response | None = None) -> None:
        resp_part = ""
        if resp:
            resp_part = f", got HTTP {resp.status_code}: {resp.text}"
        message = f"Failed to fetch {url}{resp_part}"
        super().__init__(message)
        self.resp = resp
        self.url = url


class ObjectIsGoneError(FetchError):
    pass


class ObjectNotFoundError(FetchError):
    pass


class ObjectUnavailableError(FetchError):
    pass


class FetchErrorTypeEnum(str, enum.Enum):
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"
    UNAUHTORIZED = "UNAUTHORIZED"

    INTERNAL_ERROR = "INTERNAL_ERROR"


class VisibilityEnum(str, enum.Enum):
    PUBLIC = "public"
    UNLISTED = "unlisted"
    FOLLOWERS_ONLY = "followers-only"
    DIRECT = "direct"

    @staticmethod
    def get_display_name(key: "VisibilityEnum") -> str:
        return {
            VisibilityEnum.PUBLIC: "Public - sent to followers and visible on the homepage",  # noqa: E501
            VisibilityEnum.UNLISTED: "Unlisted - like public, but hidden from the homepage",  # noqa: E501,
            VisibilityEnum.FOLLOWERS_ONLY: "Followers only",
            VisibilityEnum.DIRECT: "Direct - only visible for mentioned actors",
        }[key]


_LOCAL_ACTOR_SUMMARY, _LOCAL_ACTOR_TAGS = hashtagify(config.CONFIG.summary)
_LOCAL_ACTOR_METADATA = []
if config.CONFIG.metadata:
    for kv in config.CONFIG.metadata:
        kv_value, kv_tags = hashtagify(kv.value)
        _LOCAL_ACTOR_METADATA.append(
            {
                "name": kv.key,
                "type": "PropertyValue",
                "value": kv_value,
            }
        )
        _LOCAL_ACTOR_TAGS.extend(kv_tags)


ME = {
    "@context": AS_EXTENDED_CTX,
    "type": "Person",
    "id": config.ID,
    "following": config.BASE_URL + "/following",
    "followers": config.BASE_URL + "/followers",
    "featured": config.BASE_URL + "/featured",
    "inbox": config.BASE_URL + "/inbox",
    "outbox": config.BASE_URL + "/outbox",
    "preferredUsername": config.USERNAME,
    "name": config.CONFIG.name,
    "summary": _LOCAL_ACTOR_SUMMARY,
    "endpoints": {
        # For compat with servers expecting a sharedInbox...
        "sharedInbox": config.BASE_URL
        + "/inbox",
    },
    "url": config.ID + "/",  # XXX: the path is important for Mastodon compat
    "manuallyApprovesFollowers": config.CONFIG.manually_approves_followers,
    "attachment": _LOCAL_ACTOR_METADATA,
    "publicKey": {
        "id": f"{config.ID}#main-key",
        "owner": config.ID,
        "publicKeyPem": get_pubkey_as_pem(config.KEY_PATH),
    },
    "tag": dedup_tags(_LOCAL_ACTOR_TAGS),
}

if config.CONFIG.icon_url:
    ME["icon"] = {
        "mediaType": mimetypes.guess_type(config.CONFIG.icon_url)[0],
        "type": "Image",
        "url": config.CONFIG.icon_url,
    }

if ALSO_KNOWN_AS:
    ME["alsoKnownAs"] = [ALSO_KNOWN_AS]

if MOVED_TO:
    ME["movedTo"] = MOVED_TO

if config.CONFIG.image_url:
    ME["image"] = {
        "mediaType": mimetypes.guess_type(config.CONFIG.image_url)[0],
        "type": "Image",
        "url": config.CONFIG.image_url,
    }


class NotAnObjectError(Exception):
    def __init__(self, url: str, resp: httpx.Response | None = None) -> None:
        message = f"{url} is not an AP activity"
        super().__init__(message)
        self.url = url
        self.resp = resp


async def fetch(
    url: str,
    params: dict[str, Any] | None = None,
    disable_httpsig: bool = False,
) -> RawObject:
    logger.info(f"Fetching {url} ({params=})")
    check_url(url)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": config.AP_CONTENT_TYPE,
            },
            params=params,
            follow_redirects=True,
            auth=None if disable_httpsig else auth,
        )

    # Special handling for deleted object
    if resp.status_code == 410:
        raise ObjectIsGoneError(url, resp)
    elif resp.status_code in [401, 403]:
        raise ObjectUnavailableError(url, resp)
    elif resp.status_code == 404:
        raise ObjectNotFoundError(url, resp)

    try:
        resp.raise_for_status()
    except httpx.HTTPError as http_error:
        raise FetchError(url, resp) from http_error

    try:
        return resp.json()
    except json.JSONDecodeError:
        raise NotAnObjectError(url, resp)


async def parse_collection(  # noqa: C901
    url: str | None = None,
    payload: RawObject | None = None,
    level: int = 0,
    limit: int = 0,
) -> list[RawObject]:
    """Resolve/fetch a `Collection`/`OrderedCollection`."""
    if level > 3:
        raise ValueError("recursion limit exceeded")

    # Go through all the pages
    out: list[RawObject] = []
    if url:
        payload = await fetch(url)
    if not payload:
        raise ValueError("must at least prove a payload or an URL")

    ap_type = payload.get("type")
    if not ap_type:
        raise ValueError(f"Missing type: {payload=}")

    if level == 0 and ap_type not in ["Collection", "OrderedCollection"]:
        raise ValueError(f"Unexpected type {ap_type}")

    if payload["type"] in ["Collection", "OrderedCollection"]:
        if "orderedItems" in payload:
            return payload["orderedItems"]
        if "items" in payload:
            return payload["items"]
        if "first" in payload:
            if isinstance(payload["first"], str):
                out.extend(
                    await parse_collection(
                        url=payload["first"], level=level + 1, limit=limit
                    )
                )
            else:
                if "orderedItems" in payload["first"]:
                    out.extend(payload["first"]["orderedItems"])
                if "items" in payload["first"]:
                    out.extend(payload["first"]["items"])
                n = payload["first"].get("next")
                if n:
                    out.extend(
                        await parse_collection(url=n, level=level + 1, limit=limit)
                    )
        return out

    while payload:
        if ap_type in ["CollectionPage", "OrderedCollectionPage"]:
            if "orderedItems" in payload:
                out.extend(payload["orderedItems"])
            if "items" in payload:
                out.extend(payload["items"])
            n = payload.get("next")
            if n is None or (limit > 0 and len(out) >= limit):
                break
            payload = await fetch(n)
        else:
            raise ValueError("unexpected activity type {}".format(payload["type"]))

    return out


def as_list(val: Any | list[Any]) -> list[Any]:
    if isinstance(val, list):
        return val

    return [val]


def get_id(val: str | dict[str, Any]) -> str:
    if isinstance(val, dict):
        val = val["id"]

    if not isinstance(val, str):
        raise ValueError(f"Invalid ID type: {val}")

    return val


def object_visibility(ap_activity: RawObject, actor: "Actor") -> VisibilityEnum:
    to = as_list(ap_activity.get("to", []))
    cc = as_list(ap_activity.get("cc", []))
    if AS_PUBLIC in to:
        return VisibilityEnum.PUBLIC
    elif AS_PUBLIC in cc:
        return VisibilityEnum.UNLISTED
    elif actor.followers_collection_id and actor.followers_collection_id in to + cc:
        return VisibilityEnum.FOLLOWERS_ONLY
    else:
        return VisibilityEnum.DIRECT


def get_actor_id(activity: RawObject) -> str:
    if "attributedTo" in activity:
        attributed_to = as_list(activity["attributedTo"])
        return get_id(attributed_to[0])
    else:
        return get_id(activity["actor"])


async def get_object(activity: RawObject) -> RawObject:
    if "object" not in activity:
        raise ValueError(f"No object in {activity}")

    raw_activity_object = activity["object"]
    if isinstance(raw_activity_object, dict):
        return raw_activity_object
    elif isinstance(raw_activity_object, str):
        return await fetch(raw_activity_object)
    else:
        raise ValueError(f"Unexpected object {raw_activity_object}")


def get_object_id(activity: RawObject) -> str:
    if "object" not in activity:
        raise ValueError(f"No object in {activity}")

    return get_id(activity["object"])


def wrap_object(activity: RawObject) -> RawObject:
    # TODO(tsileo): improve Create VS Update with a `update=True` flag
    if "updated" in activity:
        return {
            "@context": AS_EXTENDED_CTX,
            "actor": config.ID,
            "to": activity.get("to", []),
            "cc": activity.get("cc", []),
            "id": activity["id"] + "/update_activity/" + activity["updated"],
            "object": remove_context(activity),
            "published": activity["published"],
            "updated": activity["updated"],
            "type": "Update",
        }
    else:
        return {
            "@context": AS_EXTENDED_CTX,
            "actor": config.ID,
            "to": activity.get("to", []),
            "cc": activity.get("cc", []),
            "id": activity["id"] + "/activity",
            "object": remove_context(activity),
            "published": activity["published"],
            "type": "Create",
        }


def wrap_object_if_needed(raw_object: RawObject) -> RawObject:
    if raw_object["type"] in ["Note", "Article", "Question"]:
        return wrap_object(raw_object)

    return raw_object


def unwrap_activity(activity: RawObject) -> RawObject:
    # FIXME(ts): deprecate this
    if activity["type"] in ["Create", "Update"]:
        unwrapped_object = activity["object"]

        # Sanity check, ensure the wrapped object actor matches the activity
        if get_actor_id(unwrapped_object) != get_actor_id(activity):
            raise ValueError(
                f"Unwrapped object actor does not match activity: {activity}"
            )
        return unwrapped_object

    return activity


def remove_context(raw_object: RawObject) -> RawObject:
    if "@context" not in raw_object:
        return raw_object
    a = dict(raw_object)
    del a["@context"]
    return a


async def post(url: str, payload: dict[str, Any]) -> httpx.Response:
    logger.info(f"Posting {url} ({payload=})")
    check_url(url)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={
                "User-Agent": config.USER_AGENT,
                "Content-Type": config.AP_CONTENT_TYPE,
            },
            json=payload,
            auth=auth,
        )
    resp.raise_for_status()
    return resp
