import enum
import json
import mimetypes
from typing import TYPE_CHECKING
from typing import Any

import httpx

from app import config
from app.config import AP_CONTENT_TYPE  # noqa: F401
from app.httpsig import auth
from app.key import get_pubkey_as_pem

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
        # toot
        "toot": "http://joinmastodon.org/ns#",
        "featured": {"@id": "toot:featured", "@type": "@id"},
        "Emoji": "toot:Emoji",
        "blurhash": "toot:blurhash",
        # schema
        "schema": "http://schema.org#",
        "PropertyValue": "schema:PropertyValue",
        "value": "schema:value",
        # ostatus
        "ostatus": "http://ostatus.org#",
        "conversation": "ostatus:conversation",
    },
]


class ObjectIsGoneError(Exception):
    pass


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
    "summary": config.CONFIG.summary,
    "endpoints": {},
    "url": config.ID,
    "manuallyApprovesFollowers": False,
    "attachment": [],
    "icon": {
        "mediaType": mimetypes.guess_type(config.CONFIG.icon_url)[0],
        "type": "Image",
        "url": config.CONFIG.icon_url,
    },
    "publicKey": {
        "id": f"{config.ID}#main-key",
        "owner": config.ID,
        "publicKeyPem": get_pubkey_as_pem(config.KEY_PATH),
    },
    "alsoKnownAs": [],
}


class NotAnObjectError(Exception):
    def __init__(self, url: str, resp: httpx.Response | None = None) -> None:
        message = f"{url} is not an AP activity"
        super().__init__(message)
        self.url = url
        self.resp = resp


async def fetch(url: str, params: dict[str, Any] | None = None) -> RawObject:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": config.AP_CONTENT_TYPE,
            },
            params=params,
            follow_redirects=True,
            auth=auth,
        )

    # Special handling for deleted object
    if resp.status_code == 410:
        raise ObjectIsGoneError(f"{url} is gone")

    resp.raise_for_status()
    try:
        return resp.json()
    except json.JSONDecodeError:
        raise NotAnObjectError(url, resp)


async def parse_collection(  # noqa: C901
    url: str | None = None,
    payload: RawObject | None = None,
    level: int = 0,
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
                    await parse_collection(url=payload["first"], level=level + 1)
                )
            else:
                if "orderedItems" in payload["first"]:
                    out.extend(payload["first"]["orderedItems"])
                if "items" in payload["first"]:
                    out.extend(payload["first"]["items"])
                n = payload["first"].get("next")
                if n:
                    out.extend(await parse_collection(url=n, level=level + 1))
        return out

    while payload:
        if ap_type in ["CollectionPage", "OrderedCollectionPage"]:
            if "orderedItems" in payload:
                out.extend(payload["orderedItems"])
            if "items" in payload:
                out.extend(payload["items"])
            n = payload.get("next")
            if n is None:
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
    if activity["type"] in ["Note", "Article", "Video", "Question", "Page"]:
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


def wrap_object(activity: RawObject) -> RawObject:
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
    if raw_object["type"] in ["Note"]:
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


def post(url: str, payload: dict[str, Any]) -> httpx.Response:
    resp = httpx.post(
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
