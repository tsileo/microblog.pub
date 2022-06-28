"""Actions related to the AP inbox/outbox."""
import uuid
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from dateutil.parser import isoparse
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app import activitypub as ap
from app import config
from app import models
from app.actor import LOCAL_ACTOR
from app.actor import Actor
from app.actor import RemoteActor
from app.actor import fetch_actor
from app.actor import save_actor
from app.ap_object import RemoteObject
from app.config import BASE_URL
from app.config import ID
from app.database import now
from app.outgoing_activities import new_outgoing_activity
from app.source import markdownify
from app.uploads import upload_to_attachment

AnyboxObject = models.InboxObject | models.OutboxObject


def allocate_outbox_id() -> str:
    return uuid.uuid4().hex


def outbox_object_id(outbox_id) -> str:
    return f"{BASE_URL}/o/{outbox_id}"


def save_outbox_object(
    db: Session,
    public_id: str,
    raw_object: ap.RawObject,
    relates_to_inbox_object_id: int | None = None,
    relates_to_outbox_object_id: int | None = None,
    relates_to_actor_id: int | None = None,
    source: str | None = None,
) -> models.OutboxObject:
    ra = RemoteObject(raw_object)

    outbox_object = models.OutboxObject(
        public_id=public_id,
        ap_type=ra.ap_type,
        ap_id=ra.ap_id,
        ap_context=ra.ap_context,
        ap_object=ra.ap_object,
        visibility=ra.visibility,
        og_meta=ra.og_meta,
        relates_to_inbox_object_id=relates_to_inbox_object_id,
        relates_to_outbox_object_id=relates_to_outbox_object_id,
        relates_to_actor_id=relates_to_actor_id,
        activity_object_ap_id=ra.activity_object_ap_id,
        is_hidden_from_homepage=True if ra.in_reply_to else False,
        source=source,
    )
    db.add(outbox_object)
    db.commit()
    db.refresh(outbox_object)

    return outbox_object


def send_like(db: Session, ap_object_id: str) -> None:
    inbox_object = get_inbox_object_by_ap_id(db, ap_object_id)
    if not inbox_object:
        raise ValueError(f"{ap_object_id} not found in the inbox")

    like_id = allocate_outbox_id()
    like = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(like_id),
        "type": "Like",
        "actor": ID,
        "object": ap_object_id,
    }
    outbox_object = save_outbox_object(
        db, like_id, like, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    inbox_object.liked_via_outbox_object_ap_id = outbox_object.ap_id
    db.commit()

    new_outgoing_activity(db, inbox_object.actor.inbox_url, outbox_object.id)


def send_announce(db: Session, ap_object_id: str) -> None:
    inbox_object = get_inbox_object_by_ap_id(db, ap_object_id)
    if not inbox_object:
        raise ValueError(f"{ap_object_id} not found in the inbox")

    announce_id = allocate_outbox_id()
    announce = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(announce_id),
        "type": "Announce",
        "actor": ID,
        "object": ap_object_id,
        "to": [ap.AS_PUBLIC],
        "cc": [
            f"{BASE_URL}/followers",
            inbox_object.ap_actor_id,
        ],
    }
    outbox_object = save_outbox_object(
        db, announce_id, announce, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    inbox_object.announced_via_outbox_object_ap_id = outbox_object.ap_id
    db.commit()

    recipients = _compute_recipients(db, announce)
    for rcp in recipients:
        new_outgoing_activity(db, rcp, outbox_object.id)


def send_follow(db: Session, ap_actor_id: str) -> None:
    actor = fetch_actor(db, ap_actor_id)

    follow_id = allocate_outbox_id()
    follow = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(follow_id),
        "type": "Follow",
        "actor": ID,
        "object": ap_actor_id,
    }

    outbox_object = save_outbox_object(
        db, follow_id, follow, relates_to_actor_id=actor.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    new_outgoing_activity(db, actor.inbox_url, outbox_object.id)


def send_undo(db: Session, ap_object_id: str) -> None:
    outbox_object_to_undo = get_outbox_object_by_ap_id(db, ap_object_id)
    if not outbox_object_to_undo:
        raise ValueError(f"{ap_object_id} not found in the outbox")

    if outbox_object_to_undo.ap_type not in ["Follow", "Like", "Announce"]:
        raise ValueError(
            f"Cannot build Undo for {outbox_object_to_undo.ap_type} activity"
        )

    undo_id = allocate_outbox_id()
    undo = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(undo_id),
        "type": "Undo",
        "actor": ID,
        "object": ap.remove_context(outbox_object_to_undo.ap_object),
    }

    outbox_object = save_outbox_object(
        db,
        undo_id,
        undo,
        relates_to_outbox_object_id=outbox_object_to_undo.id,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    outbox_object_to_undo.undone_by_outbox_object_id = outbox_object.id

    if outbox_object_to_undo.ap_type == "Follow":
        if not outbox_object_to_undo.activity_object_ap_id:
            raise ValueError("Should never happen")
        followed_actor = fetch_actor(db, outbox_object_to_undo.activity_object_ap_id)
        new_outgoing_activity(
            db,
            followed_actor.inbox_url,
            outbox_object.id,
        )
        # Also remove the follow from the following collection
        db.query(models.Following).filter(
            models.Following.ap_actor_id == followed_actor.ap_id
        ).delete()
        db.commit()
    elif outbox_object_to_undo.ap_type == "Like":
        liked_object_ap_id = outbox_object_to_undo.activity_object_ap_id
        if not liked_object_ap_id:
            raise ValueError("Should never happen")
        liked_object = get_inbox_object_by_ap_id(db, liked_object_ap_id)
        if not liked_object:
            raise ValueError(f"Cannot find liked object {liked_object_ap_id}")
        liked_object.liked_via_outbox_object_ap_id = None

        # Send the Undo to the liked object's actor
        new_outgoing_activity(
            db,
            liked_object.actor.inbox_url,  # type: ignore
            outbox_object.id,
        )
    elif outbox_object_to_undo.ap_type == "Announce":
        announced_object_ap_id = outbox_object_to_undo.activity_object_ap_id
        if not announced_object_ap_id:
            raise ValueError("Should never happen")
        announced_object = get_inbox_object_by_ap_id(db, announced_object_ap_id)
        if not announced_object:
            raise ValueError(f"Cannot find announced object {announced_object_ap_id}")
        announced_object.announced_via_outbox_object_ap_id = None

        # Send the Undo to the original recipients
        recipients = _compute_recipients(db, outbox_object.ap_object)
        for rcp in recipients:
            new_outgoing_activity(db, rcp, outbox_object.id)
    else:
        raise ValueError("Should never happen")


def send_create(
    db: Session,
    source: str,
    uploads: list[tuple[models.Upload, str]],
    in_reply_to: str | None,
    visibility: ap.VisibilityEnum,
) -> str:
    note_id = allocate_outbox_id()
    published = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    context = f"{ID}/contexts/" + uuid.uuid4().hex
    content, tags, mentioned_actors = markdownify(db, source)
    attachments = []

    if in_reply_to:
        in_reply_to_object = get_anybox_object_by_ap_id(db, in_reply_to)
        if not in_reply_to_object:
            raise ValueError(f"Invalid in reply to {in_reply_to=}")
        if not in_reply_to_object.ap_context:
            raise ValueError("Object has no context")
        context = in_reply_to_object.ap_context

        if in_reply_to_object.is_from_outbox:
            db.query(models.OutboxObject).filter(
                models.OutboxObject.ap_id == in_reply_to,
            ).update({"replies_count": models.OutboxObject.replies_count + 1})

    for (upload, filename) in uploads:
        attachments.append(upload_to_attachment(upload, filename))

    to = []
    cc = []
    mentioned_actor_ap_ids = [actor.ap_id for actor in mentioned_actors]
    if visibility == ap.VisibilityEnum.PUBLIC:
        to = [ap.AS_PUBLIC]
        cc = [f"{BASE_URL}/followers"] + mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.UNLISTED:
        to = [f"{BASE_URL}/followers"]
        cc = [ap.AS_PUBLIC] + mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.FOLLOWERS_ONLY:
        to = [f"{BASE_URL}/followers"]
        cc = mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.DIRECT:
        to = mentioned_actor_ap_ids
        cc = []
    else:
        raise ValueError(f"Unhandled visibility {visibility}")

    note = {
        "@context": ap.AS_EXTENDED_CTX,
        "type": "Note",
        "id": outbox_object_id(note_id),
        "attributedTo": ID,
        "content": content,
        "to": to,
        "cc": cc,
        "published": published,
        "context": context,
        "conversation": context,
        "url": outbox_object_id(note_id),
        "tag": tags,
        "summary": None,
        "inReplyTo": in_reply_to,
        "sensitive": False,
        "attachment": attachments,
    }
    outbox_object = save_outbox_object(db, note_id, note, source=source)
    if not outbox_object.id:
        raise ValueError("Should never happen")

    for tag in tags:
        if tag["type"] == "Hashtag":
            tagged_object = models.TaggedOutboxObject(
                tag=tag["name"][1:],
                outbox_object_id=outbox_object.id,
            )
            db.add(tagged_object)

    for (upload, filename) in uploads:
        outbox_object_attachment = models.OutboxObjectAttachment(
            filename=filename, outbox_object_id=outbox_object.id, upload_id=upload.id
        )
        db.add(outbox_object_attachment)

    db.commit()

    recipients = _compute_recipients(db, note)
    for rcp in recipients:
        new_outgoing_activity(db, rcp, outbox_object.id)

    return note_id


def _compute_recipients(db: Session, ap_object: ap.RawObject) -> set[str]:
    _recipients = []
    for field in ["to", "cc", "bto", "bcc"]:
        if field in ap_object:
            _recipients.extend(ap.as_list(ap_object[field]))

    recipients = set()
    logger.info(f"{_recipients}")
    for r in _recipients:
        if r in [ap.AS_PUBLIC, ID]:
            continue

        # If we got a local collection, assume it's a collection of actors
        if r.startswith(BASE_URL):
            for actor in fetch_actor_collection(db, r):
                recipients.add(actor.shared_inbox_url or actor.inbox_url)

            continue

        # Is it a known actor?
        known_actor = (
            db.query(models.Actor).filter(models.Actor.ap_id == r).one_or_none()
        )
        if known_actor:
            recipients.add(known_actor.shared_inbox_url or known_actor.inbox_url)
            continue

        # Fetch the object
        raw_object = ap.fetch(r)
        if raw_object.get("type") in ap.ACTOR_TYPES:
            saved_actor = save_actor(db, raw_object)
            recipients.add(saved_actor.shared_inbox_url or saved_actor.inbox_url)
        else:
            # Assume it's a collection of actors
            for raw_actor in ap.parse_collection(payload=raw_object):
                actor = RemoteActor(raw_actor)
                recipients.add(actor.shared_inbox_url or actor.inbox_url)

    return recipients


def get_inbox_object_by_ap_id(db: Session, ap_id: str) -> models.InboxObject | None:
    return (
        db.query(models.InboxObject)
        .filter(models.InboxObject.ap_id == ap_id)
        .one_or_none()
    )


def get_outbox_object_by_ap_id(db: Session, ap_id: str) -> models.OutboxObject | None:
    return (
        db.query(models.OutboxObject)
        .filter(models.OutboxObject.ap_id == ap_id)
        .one_or_none()
    )


def get_anybox_object_by_ap_id(db: Session, ap_id: str) -> AnyboxObject | None:
    if ap_id.startswith(BASE_URL):
        return get_outbox_object_by_ap_id(db, ap_id)
    else:
        return get_inbox_object_by_ap_id(db, ap_id)


def _handle_delete_activity(
    db: Session,
    from_actor: models.Actor,
    ap_object_to_delete: models.InboxObject,
) -> None:
    if from_actor.ap_id != ap_object_to_delete.actor.ap_id:
        logger.warning(
            "Actor mismatch between the activity and the object: "
            f"{from_actor.ap_id}/{ap_object_to_delete.actor.ap_id}"
        )
        return

    # TODO(ts): do we need to delete related activities? should we keep
    # bookmarked objects with a deleted flag?
    logger.info(f"Deleting {ap_object_to_delete.ap_type}/{ap_object_to_delete.ap_id}")
    db.delete(ap_object_to_delete)
    db.flush()


def _handle_follow_follow_activity(
    db: Session,
    from_actor: models.Actor,
    inbox_object: models.InboxObject,
) -> None:
    follower = models.Follower(
        actor_id=from_actor.id,
        inbox_object_id=inbox_object.id,
        ap_actor_id=from_actor.ap_id,
    )
    try:
        db.add(follower)
        db.flush()
    except IntegrityError:
        pass  # TODO update the existing followe

    # Reply with an Accept
    reply_id = allocate_outbox_id()
    reply = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(reply_id),
        "type": "Accept",
        "actor": ID,
        "object": inbox_object.ap_id,
    }
    outbox_activity = save_outbox_object(db, reply_id, reply)
    if not outbox_activity.id:
        raise ValueError("Should never happen")
    new_outgoing_activity(db, from_actor.inbox_url, outbox_activity.id)

    notif = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=from_actor.id,
    )
    db.add(notif)


def _handle_undo_activity(
    db: Session,
    from_actor: models.Actor,
    undo_activity: models.InboxObject,
    ap_activity_to_undo: models.InboxObject,
) -> None:
    if from_actor.ap_id != ap_activity_to_undo.actor.ap_id:
        logger.warning(
            "Actor mismatch between the activity and the object: "
            f"{from_actor.ap_id}/{ap_activity_to_undo.actor.ap_id}"
        )
        return

    ap_activity_to_undo.undone_by_inbox_object_id = undo_activity.id

    if ap_activity_to_undo.ap_type == "Follow":
        logger.info(f"Undo follow from {from_actor.ap_id}")
        db.query(models.Follower).filter(
            models.Follower.inbox_object_id == ap_activity_to_undo.id
        ).delete()
        notif = models.Notification(
            notification_type=models.NotificationType.UNFOLLOW,
            actor_id=from_actor.id,
        )
        db.add(notif)

    elif ap_activity_to_undo.ap_type == "Like":
        if not ap_activity_to_undo.activity_object_ap_id:
            raise ValueError("Like without object")
        liked_obj = get_outbox_object_by_ap_id(
            db,
            ap_activity_to_undo.activity_object_ap_id,
        )
        if not liked_obj:
            logger.warning(
                "Cannot find liked object: "
                f"{ap_activity_to_undo.activity_object_ap_id}"
            )
            return

        liked_obj.likes_count = models.OutboxObject.likes_count - 1
        notif = models.Notification(
            notification_type=models.NotificationType.UNDO_LIKE,
            actor_id=from_actor.id,
            outbox_object_id=liked_obj.id,
            inbox_object_id=ap_activity_to_undo.id,
        )
        db.add(notif)

    elif ap_activity_to_undo.ap_type == "Announce":
        if not ap_activity_to_undo.activity_object_ap_id:
            raise ValueError("Announce witout object")
        announced_obj_ap_id = ap_activity_to_undo.activity_object_ap_id
        logger.info(
            f"Undo for announce {ap_activity_to_undo.ap_id}/{announced_obj_ap_id}"
        )
        if announced_obj_ap_id.startswith(BASE_URL):
            announced_obj_from_outbox = get_outbox_object_by_ap_id(
                db, announced_obj_ap_id
            )
            if announced_obj_from_outbox:
                logger.info("Found in the oubox")
                announced_obj_from_outbox.announces_count = (
                    models.OutboxObject.announces_count - 1
                )
                notif = models.Notification(
                    notification_type=models.NotificationType.UNDO_ANNOUNCE,
                    actor_id=from_actor.id,
                    outbox_object_id=announced_obj_from_outbox.id,
                    inbox_object_id=ap_activity_to_undo.id,
                )
                db.add(notif)

        # FIXME(ts): what to do with ap_activity_to_undo? flag? delete?
    else:
        logger.warning(f"Don't know how to undo {ap_activity_to_undo.ap_type} activity")

    # commit will be perfomed in save_to_inbox


def _handle_create_activity(
    db: Session,
    from_actor: models.Actor,
    created_object: models.InboxObject,
) -> None:
    logger.info("Processing Create activity")
    tags = created_object.ap_object.get("tag")

    if not tags:
        logger.info("No tags to process")
        return None

    if not isinstance(tags, list):
        logger.info(f"Invalid tags: {tags}")
        return None

    if created_object.in_reply_to and created_object.in_reply_to.startswith(BASE_URL):
        db.query(models.OutboxObject).filter(
            models.OutboxObject.ap_id == created_object.in_reply_to,
        ).update({"replies_count": models.OutboxObject.replies_count + 1})

    for tag in tags:
        if tag.get("name") == LOCAL_ACTOR.handle or tag.get("href") == LOCAL_ACTOR.url:
            notif = models.Notification(
                notification_type=models.NotificationType.MENTION,
                actor_id=from_actor.id,
                inbox_object_id=created_object.id,
            )
            db.add(notif)


def save_to_inbox(db: Session, raw_object: ap.RawObject) -> None:
    try:
        actor = fetch_actor(db, raw_object["actor"])
    except httpx.HTTPStatusError:
        logger.exception("Failed to fetch actor")
        # XXX: Delete 410 when we never seen the actor
        return

    ap_published_at = now()
    if "published" in raw_object:
        ap_published_at = isoparse(raw_object["published"])

    ra = RemoteObject(ap.unwrap_activity(raw_object), actor=actor)
    relates_to_inbox_object: models.InboxObject | None = None
    relates_to_outbox_object: models.OutboxObject | None = None
    if ra.activity_object_ap_id:
        if ra.activity_object_ap_id.startswith(BASE_URL):
            relates_to_outbox_object = get_outbox_object_by_ap_id(
                db,
                ra.activity_object_ap_id,
            )
        else:
            relates_to_inbox_object = get_inbox_object_by_ap_id(
                db,
                ra.activity_object_ap_id,
            )

    inbox_object = models.InboxObject(
        server=urlparse(ra.ap_id).netloc,
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
        ap_type=ra.ap_type,
        ap_id=ra.ap_id,
        ap_context=ra.ap_context,
        ap_published_at=ap_published_at,
        ap_object=ra.ap_object,
        visibility=ra.visibility,
        relates_to_inbox_object_id=relates_to_inbox_object.id
        if relates_to_inbox_object
        else None,
        relates_to_outbox_object_id=relates_to_outbox_object.id
        if relates_to_outbox_object
        else None,
        activity_object_ap_id=ra.activity_object_ap_id,
        # Hide replies from the stream
        is_hidden_from_stream=(
            True
            if (ra.in_reply_to and not ra.in_reply_to.startswith(BASE_URL))
            else False
        ),  # TODO: handle mentions
    )

    db.add(inbox_object)
    db.flush()
    db.refresh(inbox_object)

    if ra.ap_type == "Note":  # TODO: handle create better
        _handle_create_activity(db, actor, inbox_object)
    elif ra.ap_type == "Update":
        pass
    elif ra.ap_type == "Delete":
        if relates_to_inbox_object:
            _handle_delete_activity(db, actor, relates_to_inbox_object)
        else:
            # TODO(ts): handle delete actor
            logger.info(
                f"Received a Delete for an unknown object: {ra.activity_object_ap_id}"
            )
    elif ra.ap_type == "Follow":
        _handle_follow_follow_activity(db, actor, inbox_object)
    elif ra.ap_type == "Undo":
        if relates_to_inbox_object:
            _handle_undo_activity(db, actor, inbox_object, relates_to_inbox_object)
        else:
            logger.info("Received Undo for an unknown activity")
    elif ra.ap_type in ["Accept", "Reject"]:
        if not relates_to_outbox_object:
            logger.info(
                f"Received {raw_object['type']} for an unknown activity: "
                f"{ra.activity_object_ap_id}"
            )
        else:
            if relates_to_outbox_object.ap_type == "Follow":
                following = models.Following(
                    actor_id=actor.id,
                    outbox_object_id=relates_to_outbox_object.id,
                    ap_actor_id=actor.ap_id,
                )
                db.add(following)
            else:
                logger.info(
                    "Received an Accept for an unsupported activity: "
                    f"{relates_to_outbox_object.ap_type}"
                )
    elif ra.ap_type == "EmojiReact":
        if not relates_to_outbox_object:
            logger.info(
                f"Received a like for an unknown activity: {ra.activity_object_ap_id}"
            )
        else:
            # TODO(ts): support reactions
            pass
    elif ra.ap_type == "Like":
        if not relates_to_outbox_object:
            logger.info(
                f"Received a like for an unknown activity: {ra.activity_object_ap_id}"
            )
        else:
            relates_to_outbox_object.likes_count = models.OutboxObject.likes_count + 1

            notif = models.Notification(
                notification_type=models.NotificationType.LIKE,
                actor_id=actor.id,
                outbox_object_id=relates_to_outbox_object.id,
                inbox_object_id=inbox_object.id,
            )
            db.add(notif)
    elif raw_object["type"] == "Announce":
        if relates_to_outbox_object:
            # This is an announce for a local object
            relates_to_outbox_object.announces_count = (
                models.OutboxObject.announces_count + 1
            )

            notif = models.Notification(
                notification_type=models.NotificationType.ANNOUNCE,
                actor_id=actor.id,
                outbox_object_id=relates_to_outbox_object.id,
                inbox_object_id=inbox_object.id,
            )
            db.add(notif)
        else:
            # This is announce for a maybe unknown object
            if relates_to_inbox_object:
                logger.info("Nothing to do, we already know about this object")
            else:
                # Save it as an inbox object
                if not ra.activity_object_ap_id:
                    raise ValueError("Should never happen")
                announced_raw_object = ap.fetch(ra.activity_object_ap_id)
                announced_actor = fetch_actor(db, ap.get_actor_id(announced_raw_object))
                announced_object = RemoteObject(announced_raw_object, announced_actor)
                announced_inbox_object = models.InboxObject(
                    server=urlparse(announced_object.ap_id).netloc,
                    actor_id=announced_actor.id,
                    ap_actor_id=announced_actor.ap_id,
                    ap_type=announced_object.ap_type,
                    ap_id=announced_object.ap_id,
                    ap_context=announced_object.ap_context,
                    ap_published_at=announced_object.ap_published_at,
                    ap_object=announced_object.ap_object,
                    visibility=announced_object.visibility,
                    is_hidden_from_stream=True,
                )
                db.add(announced_inbox_object)
                db.flush()
                inbox_object.relates_to_inbox_object_id = announced_inbox_object.id
    elif ra.ap_type in ["Like", "Announce"]:
        if not relates_to_outbox_object:
            logger.info(
                f"Received {ra.ap_type} for an unknown activity: "
                f"{ra.activity_object_ap_id}"
            )
        else:
            if ra.ap_type == "Like":
                # TODO(ts): notification
                relates_to_outbox_object.likes_count = (
                    models.OutboxObject.likes_count + 1
                )

                notif = models.Notification(
                    notification_type=models.NotificationType.LIKE,
                    actor_id=actor.id,
                    outbox_object_id=relates_to_outbox_object.id,
                    inbox_object_id=inbox_object.id,
                )
                db.add(notif)
            elif raw_object["type"] == "Announce":
                # TODO(ts): notification
                relates_to_outbox_object.announces_count = (
                    models.OutboxObject.announces_count + 1
                )

                notif = models.Notification(
                    notification_type=models.NotificationType.ANNOUNCE,
                    actor_id=actor.id,
                    outbox_object_id=relates_to_outbox_object.id,
                    inbox_object_id=inbox_object.id,
                )
                db.add(notif)
            else:
                raise ValueError("Should never happen")

    else:
        logger.warning(f"Received an unknown {inbox_object.ap_type} object")

    db.commit()


def public_outbox_objects_count(db: Session) -> int:
    return (
        db.query(models.OutboxObject)
        .filter(
            models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
            models.OutboxObject.is_deleted.is_(False),
        )
        .count()
    )


def fetch_actor_collection(db: Session, url: str) -> list[Actor]:
    if url.startswith(config.BASE_URL):
        if url == config.BASE_URL + "/followers":
            q = db.query(models.Follower).options(joinedload(models.Follower.actor))
            return [follower.actor for follower in q.all()]
        else:
            raise ValueError(f"internal collection for {url}) not supported")

    return [RemoteActor(actor) for actor in ap.parse_collection(url)]


@dataclass
class ReplyTreeNode:
    ap_object: AnyboxObject
    children: list["ReplyTreeNode"]
    is_requested: bool = False
    is_root: bool = False


def get_replies_tree(
    db: Session,
    requested_object: AnyboxObject,
) -> ReplyTreeNode:
    # TODO: handle visibility
    tree_nodes: list[AnyboxObject] = []
    tree_nodes.extend(
        db.query(models.InboxObject)
        .filter(
            models.InboxObject.ap_context == requested_object.ap_context,
        )
        .all()
    )
    tree_nodes.extend(
        db.query(models.OutboxObject)
        .filter(
            models.OutboxObject.ap_context == requested_object.ap_context,
            models.OutboxObject.is_deleted.is_(False),
        )
        .all()
    )
    nodes_by_in_reply_to = defaultdict(list)
    for node in tree_nodes:
        nodes_by_in_reply_to[node.in_reply_to].append(node)
    logger.info(nodes_by_in_reply_to)

    # TODO: get oldest if we cannot get to root?
    if len(nodes_by_in_reply_to.get(None, [])) != 1:
        raise ValueError("Failed to compute replies tree")

    def _get_reply_node_children(
        node: ReplyTreeNode,
        index: defaultdict[str | None, list[AnyboxObject]],
    ) -> list[ReplyTreeNode]:
        children = []
        for child in index.get(node.ap_object.ap_id, []):  # type: ignore
            child_node = ReplyTreeNode(
                ap_object=child,
                is_requested=child.ap_id == requested_object.ap_id,  # type: ignore
                children=[],
            )
            child_node.children = _get_reply_node_children(child_node, index)
            children.append(child_node)

        return sorted(
            children,
            key=lambda node: node.ap_object.ap_published_at,  # type: ignore
        )

    if None in nodes_by_in_reply_to:
        root_ap_object = nodes_by_in_reply_to[None][0]
    else:
        root_ap_object = sorted(
            tree_nodes,
            lambda ap_obj: ap_obj.ap_published_at,  # type: ignore
        )[0]

    root_node = ReplyTreeNode(
        ap_object=root_ap_object,
        is_root=True,
        is_requested=root_ap_object.ap_id == requested_object.ap_id,
        children=[],
    )
    root_node.children = _get_reply_node_children(root_node, nodes_by_in_reply_to)
    return root_node
