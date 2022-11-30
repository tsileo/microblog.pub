import hashlib
import mimetypes
from datetime import datetime
from functools import cached_property
from typing import Any

import pydantic
from bs4 import BeautifulSoup  # type: ignore
from mistletoe import markdown  # type: ignore

from app import activitypub as ap
from app.actor import LOCAL_ACTOR
from app.actor import Actor
from app.actor import RemoteActor
from app.config import ID
from app.media import proxied_media_url
from app.utils.datetime import now
from app.utils.datetime import parse_isoformat


class Object:
    @property
    def is_from_db(self) -> bool:
        return False

    @property
    def is_from_outbox(self) -> bool:
        return False

    @property
    def is_from_inbox(self) -> bool:
        return False

    @cached_property
    def ap_type(self) -> str:
        return ap.as_list(self.ap_object["type"])[0]

    @property
    def ap_object(self) -> ap.RawObject:
        raise NotImplementedError

    @property
    def ap_id(self) -> str:
        return ap.get_id(self.ap_object["id"])

    @property
    def ap_actor_id(self) -> str:
        return ap.get_actor_id(self.ap_object)

    @cached_property
    def ap_published_at(self) -> datetime | None:
        # TODO: default to None? or now()?
        if "published" in self.ap_object:
            return parse_isoformat(self.ap_object["published"])
        elif "created" in self.ap_object:
            return parse_isoformat(self.ap_object["created"])
        return None

    @property
    def actor(self) -> Actor:
        raise NotImplementedError()

    @cached_property
    def visibility(self) -> ap.VisibilityEnum:
        return ap.object_visibility(self.ap_object, self.actor)

    @property
    def ap_context(self) -> str | None:
        return self.ap_object.get("context") or self.ap_object.get("conversation")

    @property
    def sensitive(self) -> bool:
        return self.ap_object.get("sensitive", False)

    @property
    def tags(self) -> list[ap.RawObject]:
        return ap.as_list(self.ap_object.get("tag", []))

    @cached_property
    def inlined_images(self) -> set[str]:
        image_urls: set[str] = set()
        if not self.content:
            return image_urls

        soup = BeautifulSoup(self.content, "html5lib")
        imgs = soup.find_all("img")

        for img in imgs:
            if not img.attrs.get("src"):
                continue

            image_urls.add(img.attrs["src"])

        return image_urls

    @cached_property
    def attachments(self) -> list["Attachment"]:
        attachments = []
        for obj in ap.as_list(self.ap_object.get("attachment", [])):
            if obj.get("type") == "PropertyValue":
                continue

            if obj.get("type") == "Link":
                attachments.append(
                    Attachment.parse_obj(
                        {
                            "proxiedUrl": None,
                            "resizedUrl": None,
                            "mediaType": None,
                            "type": "Link",
                            "url": obj["href"],
                        }
                    )
                )
                continue

            proxied_url = proxied_media_url(obj["url"])
            attachments.append(
                Attachment.parse_obj(
                    {
                        "proxiedUrl": proxied_url,
                        "resizedUrl": proxied_url + "/740"
                        if obj.get("mediaType", "").startswith("image")
                        else None,
                        **obj,
                    }
                )
            )

        # Also add any video Link (for PeerTube compat)
        if self.ap_type == "Video":
            for link in ap.as_list(self.ap_object.get("url", [])):
                if (isinstance(link, dict)) and link.get("type") == "Link":
                    if link.get("mediaType", "").startswith("video"):
                        proxied_url = proxied_media_url(link["href"])
                        attachments.append(
                            Attachment(
                                type="Video",
                                mediaType=link["mediaType"],
                                url=link["href"],
                                proxiedUrl=proxied_url,
                            )
                        )
                        break
                    elif link.get("mediaType", "") == "application/x-mpegURL":
                        for tag in ap.as_list(link.get("tag", [])):
                            if tag.get("mediaType", "").startswith("video"):
                                proxied_url = proxied_media_url(tag["href"])
                                attachments.append(
                                    Attachment(
                                        type="Video",
                                        mediaType=tag["mediaType"],
                                        url=tag["href"],
                                        proxiedUrl=proxied_url,
                                    )
                                )
                                break
        return attachments

    @cached_property
    def url(self) -> str | None:
        obj_url = self.ap_object.get("url")
        if isinstance(obj_url, str) and obj_url:
            return obj_url
        elif obj_url:
            for u in ap.as_list(obj_url):
                if u.get("type") == "Link":
                    return u["href"]

                if u["mediaType"] == "text/html":
                    return u["href"]

        return self.ap_id

    @cached_property
    def content(self) -> str | None:
        content = self.ap_object.get("content")
        if not content:
            return None

        # PeerTube returns the content as markdown
        if self.ap_object.get("mediaType") == "text/markdown":
            content = markdown(content)

        return content

    @property
    def summary(self) -> str | None:
        return self.ap_object.get("summary")

    @property
    def name(self) -> str | None:
        return self.ap_object.get("name")

    @cached_property
    def permalink_id(self) -> str:
        return (
            "permalink-"
            + hashlib.md5(
                self.ap_id.encode(),
                usedforsecurity=False,
            ).hexdigest()
        )

    @property
    def activity_object_ap_id(self) -> str | None:
        if "object" in self.ap_object:
            return ap.get_id(self.ap_object["object"])

        return None

    @property
    def in_reply_to(self) -> str | None:
        return self.ap_object.get("inReplyTo")

    @property
    def is_local_reply(self) -> bool:
        if not self.in_reply_to:
            return False

        return bool(
            self.in_reply_to.startswith(ID) and self.content  # Hide votes from Question
        )

    @property
    def is_in_reply_to_from_inbox(self) -> bool | None:
        if not self.in_reply_to:
            return None

        return not self.in_reply_to.startswith(LOCAL_ACTOR.ap_id)

    @property
    def has_ld_signature(self) -> bool:
        return bool(self.ap_object.get("signature"))

    @property
    def is_poll_ended(self) -> bool:
        if self.poll_end_time:
            return now() > self.poll_end_time
        return False

    @cached_property
    def poll_items(self) -> list[ap.RawObject] | None:
        return self.ap_object.get("oneOf") or self.ap_object.get("anyOf")

    @cached_property
    def poll_end_time(self) -> datetime | None:
        # Some polls may not have an end time
        if self.ap_object.get("endTime"):
            return parse_isoformat(self.ap_object["endTime"])

        return None

    @cached_property
    def poll_voters_count(self) -> int | None:
        if not self.poll_items:
            return None
        # Only Mastodon set this attribute
        if self.ap_object.get("votersCount"):
            return self.ap_object["votersCount"]
        else:
            voters_count = 0
            for item in self.poll_items:
                voters_count += item.get("replies", {}).get("totalItems", 0)

            return voters_count

    @cached_property
    def is_one_of_poll(self) -> bool:
        return bool(self.ap_object.get("oneOf"))


def _to_camel(string: str) -> str:
    cased = "".join(word.capitalize() for word in string.split("_"))
    return cased[0:1].lower() + cased[1:]


class BaseModel(pydantic.BaseModel):
    class Config:
        alias_generator = _to_camel


class Attachment(BaseModel):
    type: str
    media_type: str | None
    name: str | None
    url: str

    # Extra fields for the templates (and only for media)
    proxied_url: str | None = None
    resized_url: str | None = None

    width: int | None = None
    height: int | None = None

    @property
    def mimetype(self) -> str:
        mimetype = self.media_type
        if not mimetype:
            mimetype, _ = mimetypes.guess_type(self.url)

        if not mimetype:
            return "unknown"

        return mimetype.split("/")[-1]


class RemoteObject(Object):
    def __init__(self, raw_object: ap.RawObject, actor: Actor):
        self._raw_object = raw_object
        self._actor = actor

        if self._actor.ap_id != ap.get_actor_id(self._raw_object):
            raise ValueError(f"Invalid actor {self._actor.ap_id}")

    @classmethod
    async def from_raw_object(
        cls,
        raw_object: ap.RawObject,
        actor: Actor | None = None,
    ):
        # Pre-fetch the actor
        actor_id = ap.get_actor_id(raw_object)
        if actor_id == LOCAL_ACTOR.ap_id:
            _actor = LOCAL_ACTOR
        elif actor:
            if actor.ap_id != actor_id:
                raise ValueError(
                    f"Invalid actor, got {actor.ap_id}, " f"expected {actor_id}"
                )
            _actor = actor  # type: ignore
        else:
            _actor = RemoteActor(
                ap_actor=await ap.fetch(ap.get_actor_id(raw_object)),
            )

        return cls(raw_object, _actor)

    @property
    def og_meta(self) -> list[dict[str, Any]] | None:
        return None

    @property
    def ap_object(self) -> ap.RawObject:
        return self._raw_object

    @property
    def actor(self) -> Actor:
        return self._actor
