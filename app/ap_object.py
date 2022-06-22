import hashlib
from datetime import datetime
from typing import Any

import pydantic
from dateutil.parser import isoparse
from markdown import markdown

from app import activitypub as ap
from app import opengraph
from app.actor import LOCAL_ACTOR
from app.actor import Actor
from app.actor import RemoteActor


class Object:
    @property
    def is_from_db(self) -> bool:
        return False

    @property
    def ap_type(self) -> str:
        return self.ap_object["type"]

    @property
    def ap_object(self) -> ap.RawObject:
        raise NotImplementedError

    @property
    def ap_id(self) -> str:
        return ap.get_id(self.ap_object["id"])

    @property
    def ap_actor_id(self) -> str:
        return ap.get_actor_id(self.ap_object)

    @property
    def ap_published_at(self) -> datetime | None:
        # TODO: default to None? or now()?
        if "published" in self.ap_object:
            return isoparse(self.ap_object["published"])
        elif "created" in self.ap_object:
            return isoparse(self.ap_object["created"])
        return None

    @property
    def actor(self) -> Actor:
        raise NotImplementedError()

    @property
    def visibility(self) -> ap.VisibilityEnum:
        return ap.object_visibility(self.ap_object)

    @property
    def context(self) -> str | None:
        return self.ap_object.get("context")

    @property
    def sensitive(self) -> bool:
        return self.ap_object.get("sensitive", False)

    @property
    def attachments(self) -> list["Attachment"]:
        attachments = [
            Attachment.parse_obj(obj) for obj in self.ap_object.get("attachment", [])
        ]

        # Also add any video Link (for PeerTube compat)
        if self.ap_type == "Video":
            for link in ap.as_list(self.ap_object.get("url", [])):
                if (isinstance(link, dict)) and link.get("type") == "Link":
                    if link.get("mediaType", "").startswith("video"):
                        attachments.append(
                            Attachment(
                                type="Video",
                                mediaType=link["mediaType"],
                                url=link["href"],
                            )
                        )
                        break

        return attachments

    @property
    def url(self) -> str | None:
        obj_url = self.ap_object.get("url")
        if isinstance(obj_url, str):
            return obj_url
        elif obj_url:
            for u in ap.as_list(obj_url):
                if u["mediaType"] == "text/html":
                    return u["href"]

        return None

    @property
    def content(self) -> str | None:
        content = self.ap_object.get("content")
        if not content:
            return None

        # PeerTube returns the content as markdown
        if self.ap_object.get("mediaType") == "text/markdown":
            return markdown(content, extensions=["mdx_linkify"])

        return content

    @property
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


def _to_camel(string: str) -> str:
    cased = "".join(word.capitalize() for word in string.split("_"))
    return cased[0:1].lower() + cased[1:]


class BaseModel(pydantic.BaseModel):
    class Config:
        alias_generator = _to_camel


class Attachment(BaseModel):
    type: str
    media_type: str
    name: str | None
    url: str


class RemoteObject(Object):
    def __init__(self, raw_object: ap.RawObject, actor: Actor | None = None):
        self._raw_object = raw_object
        self._actor: Actor

        # Pre-fetch the actor
        actor_id = ap.get_actor_id(raw_object)
        if actor_id == LOCAL_ACTOR.ap_id:
            self._actor = LOCAL_ACTOR
        elif actor:
            if actor.ap_id != actor_id:
                raise ValueError(
                    f"Invalid actor, got {actor.ap_id}, " f"expected {actor_id}"
                )
            self._actor = actor
        else:
            self._actor = RemoteActor(
                ap_actor=ap.fetch(ap.get_actor_id(raw_object)),
            )

        self._og_meta = None
        if self.ap_type == "Note":
            self._og_meta = opengraph.og_meta_from_note(self._raw_object)

    @property
    def og_meta(self) -> list[dict[str, Any]] | None:
        if self._og_meta:
            return [og_meta.dict() for og_meta in self._og_meta]
        return None

    @property
    def ap_object(self) -> ap.RawObject:
        return self._raw_object

    @property
    def actor(self) -> Actor:
        return self._actor
