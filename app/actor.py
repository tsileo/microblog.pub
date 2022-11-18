import hashlib
import typing
from dataclasses import dataclass
from datetime import timedelta
from functools import cached_property
from typing import Union
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app import activitypub as ap
from app import media
from app.config import BASE_URL
from app.database import AsyncSession
from app.utils.datetime import as_utc
from app.utils.datetime import now

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
    def outbox_url(self) -> str:
        return self.ap_actor["outbox"]

    @property
    def shared_inbox_url(self) -> str:
        return self.ap_actor.get("endpoints", {}).get("sharedInbox") or self.inbox_url

    @property
    def icon_url(self) -> str | None:
        if icon := self.ap_actor.get("icon"):
            return icon.get("url")
        return None

    @property
    def icon_media_type(self) -> str | None:
        if icon := self.ap_actor.get("icon"):
            return icon.get("mediaType")
        return None

    @property
    def image_url(self) -> str | None:
        if image := self.ap_actor.get("image"):
            return image.get("url")
        return None

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
            return BASE_URL + "/static/nopic.png"

    @property
    def resized_icon_url(self) -> str:
        if self.icon_url:
            return media.resized_media_url(self.icon_url, 50)
        else:
            return BASE_URL + "/static/nopic.png"

    @property
    def tags(self) -> list[ap.RawObject]:
        return ap.as_list(self.ap_actor.get("tag", []))

    @property
    def followers_collection_id(self) -> str | None:
        return self.ap_actor.get("followers")

    @cached_property
    def attachments(self) -> list[ap.RawObject]:
        return ap.as_list(self.ap_actor.get("attachment", []))

    @cached_property
    def moved_to(self) -> str | None:
        return self.ap_actor.get("movedTo")

    @cached_property
    def server(self) -> str:
        return urlparse(self.ap_id).hostname  # type: ignore


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
        ap_id=ap.get_id(ap_actor["id"]),
        ap_actor=ap_actor,
        ap_type=ap.as_list(ap_actor["type"])[0],
        handle=_handle(ap_actor),
    )
    db_session.add(actor)
    await db_session.flush()
    await db_session.refresh(actor)
    return actor


async def fetch_actor(
    db_session: AsyncSession,
    actor_id: str,
    save_if_not_found: bool = True,
) -> "ActorModel":
    if actor_id == LOCAL_ACTOR.ap_id:
        raise ValueError("local actor should not be fetched")
    from app import models

    existing_actor = (
        await db_session.scalars(
            select(models.Actor).where(
                models.Actor.ap_id == actor_id,
            )
        )
    ).one_or_none()
    if existing_actor:
        if existing_actor.is_deleted:
            raise ap.ObjectNotFoundError(f"{actor_id} was deleted")

        if now() - as_utc(existing_actor.updated_at) > timedelta(hours=24):
            logger.info(
                f"Refreshing {actor_id=} last updated {existing_actor.updated_at}"
            )
            try:
                ap_actor = await ap.fetch(actor_id)
                await update_actor_if_needed(
                    db_session,
                    existing_actor,
                    RemoteActor(ap_actor),
                )
                return existing_actor
            except Exception:
                logger.exception(f"Failed to refresh {actor_id}")
                # If we fail to refresh the actor, return the cached one
                return existing_actor
        else:
            return existing_actor

    if save_if_not_found:
        ap_actor = await ap.fetch(actor_id)
        # Some softwares uses URL when we expect ID or uses a different casing
        # (like Birdsite LIVE) , which mean we may already have it in DB
        existing_actor_by_url = (
            await db_session.scalars(
                select(models.Actor).where(
                    models.Actor.ap_id == ap.get_id(ap_actor),
                )
            )
        ).one_or_none()
        if existing_actor_by_url:
            # Update the actor as we had to fetch it anyway
            await update_actor_if_needed(
                db_session,
                existing_actor_by_url,
                RemoteActor(ap_actor),
            )
            return existing_actor_by_url

        return await save_actor(db_session, ap_actor)
    else:
        raise ap.ObjectNotFoundError(actor_id)


async def update_actor_if_needed(
    db_session: AsyncSession,
    actor_in_db: "ActorModel",
    ra: RemoteActor,
) -> None:
    # Check if we actually need to udpte the actor in DB
    if _actor_hash(ra) != _actor_hash(actor_in_db):
        actor_in_db.ap_actor = ra.ap_actor
        actor_in_db.handle = ra.handle
        actor_in_db.ap_type = ra.ap_type

    actor_in_db.updated_at = now()
    await db_session.flush()


@dataclass
class ActorMetadata:
    ap_actor_id: str
    is_following: bool
    is_follower: bool
    is_follow_request_sent: bool
    is_follow_request_rejected: bool
    outbox_follow_ap_id: str | None
    inbox_follow_ap_id: str | None
    moved_to: typing.Optional["ActorModel"]
    has_blocked_local_actor: bool


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
    rejected_follow_requests = {
        reject.activity_object_ap_id
        for reject in await db_session.execute(
            select(models.InboxObject.activity_object_ap_id).where(
                models.InboxObject.ap_type == "Reject",
                models.InboxObject.ap_actor_id.in_(ap_actor_ids),
            )
        )
    }
    blocks = {
        block.ap_actor_id
        for block in await db_session.execute(
            select(models.InboxObject.ap_actor_id).where(
                models.InboxObject.ap_type == "Block",
                models.InboxObject.undone_by_inbox_object_id.is_(None),
                models.InboxObject.ap_actor_id.in_(ap_actor_ids),
            )
        )
    }

    idx: ActorsMetadata = {}
    for actor in actors:
        if not actor.ap_id:
            raise ValueError("Should never happen")
        moved_to = None
        if actor.moved_to:
            try:
                moved_to = await fetch_actor(
                    db_session,
                    actor.moved_to,
                    save_if_not_found=False,
                )
            except ap.ObjectNotFoundError:
                pass
            except Exception:
                logger.exception(f"Failed to fetch {actor.moved_to=}")

        idx[actor.ap_id] = ActorMetadata(
            ap_actor_id=actor.ap_id,
            is_following=actor.ap_id in following,
            is_follower=actor.ap_id in followers,
            is_follow_request_sent=actor.ap_id in sent_follow_requests,
            is_follow_request_rejected=bool(
                sent_follow_requests[actor.ap_id] in rejected_follow_requests
            )
            if actor.ap_id in sent_follow_requests
            else False,
            outbox_follow_ap_id=sent_follow_requests.get(actor.ap_id),
            inbox_follow_ap_id=followers.get(actor.ap_id),
            moved_to=moved_to,
            has_blocked_local_actor=actor.ap_id in blocks,
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

    if actor.image_url:
        h.update(actor.image_url.encode())

    if actor.attachments:
        for a in actor.attachments:
            if a.get("type") != "PropertyValue":
                continue

            h.update(a["name"].encode())
            h.update(a["value"].encode())

    h.update(actor.public_key_id.encode())
    h.update(actor.public_key_as_pem.encode())

    if actor.moved_to:
        h.update(actor.moved_to.encode())

    return h.digest()
