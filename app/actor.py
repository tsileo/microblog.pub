import hashlib
import typing
from dataclasses import dataclass
from functools import cached_property
from typing import Union
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app import activitypub as ap
from app import media
from app.database import AsyncSession

if typing.TYPE_CHECKING:
    from app.models import Actor as ActorModel


def _handle(raw_actor: ap.RawObject) -> str:
    ap_id = ap.get_id(raw_actor["id"])
    domain = urlparse(ap_id)
    if not domain.hostname:
        raise ValueError(f"Invalid actor ID {ap_id}")

    return f'@{raw_actor["preferredUsername"]}@{domain.hostname}'  # type: ignore


class Actor:
    @property
    def ap_actor(self) -> ap.RawObject:
        raise NotImplementedError()

    @property
    def ap_id(self) -> str:
        return ap.get_id(self.ap_actor["id"])

    @property
    def name(self) -> str | None:
        return self.ap_actor.get("name")

    @property
    def summary(self) -> str | None:
        return self.ap_actor.get("summary")

    @property
    def url(self) -> str | None:
        return self.ap_actor.get("url") or self.ap_actor["id"]

    @property
    def preferred_username(self) -> str:
        return self.ap_actor["preferredUsername"]

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        return self.preferred_username

    @property
    def handle(self) -> str:
        return _handle(self.ap_actor)

    @property
    def ap_type(self) -> str:
        raise NotImplementedError()

    @property
    def inbox_url(self) -> str:
        return self.ap_actor["inbox"]

    @property
    def shared_inbox_url(self) -> str:
        return self.ap_actor.get("endpoints", {}).get("sharedInbox") or self.inbox_url

    @property
    def icon_url(self) -> str | None:
        return self.ap_actor.get("icon", {}).get("url")

    @property
    def icon_media_type(self) -> str | None:
        return self.ap_actor.get("icon", {}).get("mediaType")

    @property
    def public_key_as_pem(self) -> str:
        return self.ap_actor["publicKey"]["publicKeyPem"]

    @property
    def public_key_id(self) -> str:
        return self.ap_actor["publicKey"]["id"]

    @property
    def proxied_icon_url(self) -> str:
        if self.icon_url:
            return media.proxied_media_url(self.icon_url)
        else:
            return "/static/nopic.png"

    @property
    def resized_icon_url(self) -> str:
        if self.icon_url:
            return media.resized_media_url(self.icon_url, 50)
        else:
            return "/static/nopic.png"

    @property
    def tags(self) -> list[ap.RawObject]:
        return self.ap_actor.get("tag", [])

    @property
    def followers_collection_id(self) -> str | None:
        return self.ap_actor.get("followers")

    @cached_property
    def attachments(self) -> list[ap.RawObject]:
        return ap.as_list(self.ap_actor.get("attachment", []))


class RemoteActor(Actor):
    def __init__(self, ap_actor: ap.RawObject) -> None:
        if (ap_type := ap_actor.get("type")) not in ap.ACTOR_TYPES:
            raise ValueError(f"Unexpected actor type: {ap_type}")

        self._ap_actor = ap_actor
        self._ap_type = ap_type

    @property
    def ap_actor(self) -> ap.RawObject:
        return self._ap_actor

    @property
    def ap_type(self) -> str:
        return self._ap_type

    @property
    def is_from_db(self) -> bool:
        return False


LOCAL_ACTOR = RemoteActor(ap_actor=ap.ME)


async def save_actor(db_session: AsyncSession, ap_actor: ap.RawObject) -> "ActorModel":
    from app import models

    if ap_type := ap_actor.get("type") not in ap.ACTOR_TYPES:
        raise ValueError(f"Invalid type {ap_type} for actor {ap_actor}")

    actor = models.Actor(
        ap_id=ap_actor["id"],
        ap_actor=ap_actor,
        ap_type=ap_actor["type"],
        handle=_handle(ap_actor),
    )
    db_session.add(actor)
    await db_session.flush()
    await db_session.refresh(actor)
    return actor


async def fetch_actor(db_session: AsyncSession, actor_id: str) -> "ActorModel":
    from app import models

    existing_actor = (
        await db_session.scalars(
            select(models.Actor).where(models.Actor.ap_id == actor_id)
        )
    ).one_or_none()
    if existing_actor:
        return existing_actor

    ap_actor = await ap.fetch(actor_id)
    return await save_actor(db_session, ap_actor)


@dataclass
class ActorMetadata:
    ap_actor_id: str
    is_following: bool
    is_follower: bool
    is_follow_request_sent: bool
    outbox_follow_ap_id: str | None
    inbox_follow_ap_id: str | None


ActorsMetadata = dict[str, ActorMetadata]


async def get_actors_metadata(
    db_session: AsyncSession,
    actors: list[Union["ActorModel", "RemoteActor"]],
) -> ActorsMetadata:
    from app import models

    ap_actor_ids = [actor.ap_id for actor in actors]
    followers = {
        follower.ap_actor_id: follower.inbox_object.ap_id
        for follower in (
            await db_session.scalars(
                select(models.Follower)
                .where(models.Follower.ap_actor_id.in_(ap_actor_ids))
                .options(joinedload(models.Follower.inbox_object))
            )
        )
        .unique()
        .all()
    }
    following = {
        following.ap_actor_id
        for following in await db_session.execute(
            select(models.Following.ap_actor_id).where(
                models.Following.ap_actor_id.in_(ap_actor_ids)
            )
        )
    }
    sent_follow_requests = {
        follow_req.ap_object["object"]: follow_req.ap_id
        for follow_req in await db_session.execute(
            select(models.OutboxObject.ap_object, models.OutboxObject.ap_id).where(
                models.OutboxObject.ap_type == "Follow",
                models.OutboxObject.undone_by_outbox_object_id.is_(None),
                models.OutboxObject.activity_object_ap_id.in_(ap_actor_ids),
            )
        )
    }
    idx: ActorsMetadata = {}
    for actor in actors:
        if not actor.ap_id:
            raise ValueError("Should never happen")
        idx[actor.ap_id] = ActorMetadata(
            ap_actor_id=actor.ap_id,
            is_following=actor.ap_id in following,
            is_follower=actor.ap_id in followers,
            is_follow_request_sent=actor.ap_id in sent_follow_requests,
            outbox_follow_ap_id=sent_follow_requests.get(actor.ap_id),
            inbox_follow_ap_id=followers.get(actor.ap_id),
        )
    return idx


def _actor_hash(actor: Actor) -> bytes:
    """Used to detect when an actor is updated"""
    h = hashlib.blake2b(digest_size=32)
    h.update(actor.ap_id.encode())
    h.update(actor.handle.encode())

    if actor.name:
        h.update(actor.name.encode())

    if actor.summary:
        h.update(actor.summary.encode())

    if actor.url:
        h.update(actor.url.encode())

    h.update(actor.display_name.encode())

    if actor.icon_url:
        h.update(actor.icon_url.encode())

    if actor.attachments:
        for a in actor.attachments:
            if a.get("type") != "PropertyValue":
                continue

            h.update(a["name"].encode())
            h.update(a["value"].encode())

    h.update(actor.public_key_id.encode())
    h.update(actor.public_key_as_pem.encode())

    return h.digest()
