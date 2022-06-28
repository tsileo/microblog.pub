import enum
from typing import Any
from typing import Optional
from typing import Union

from sqlalchemy import JSON
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import relationship

from app import activitypub as ap
from app.actor import LOCAL_ACTOR
from app.actor import Actor as BaseActor
from app.ap_object import Attachment
from app.ap_object import Object as BaseObject
from app.config import BASE_URL
from app.database import Base
from app.database import now


class Actor(Base, BaseActor):
    __tablename__ = "actor"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    ap_id = Column(String, unique=True, nullable=False, index=True)
    ap_actor: Mapped[ap.RawObject] = Column(JSON, nullable=False)
    ap_type = Column(String, nullable=False)

    handle = Column(String, nullable=True, index=True)

    @property
    def is_from_db(self) -> bool:
        return True


class InboxObject(Base, BaseObject):
    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=False)
    actor: Mapped[Actor] = relationship(Actor, uselist=False)

    server = Column(String, nullable=False)

    is_hidden_from_stream = Column(Boolean, nullable=False, default=False)

    ap_actor_id = Column(String, nullable=False)
    ap_type = Column(String, nullable=False, index=True)
    ap_id = Column(String, nullable=False, unique=True, index=True)
    ap_context = Column(String, nullable=True)
    ap_published_at = Column(DateTime(timezone=True), nullable=False)
    ap_object: Mapped[ap.RawObject] = Column(JSON, nullable=False)

    # Only set for activities
    activity_object_ap_id = Column(String, nullable=True, index=True)

    visibility = Column(Enum(ap.VisibilityEnum), nullable=False)

    # Used for Like, Announce and Undo activities
    relates_to_inbox_object_id = Column(
        Integer,
        ForeignKey("inbox.id"),
        nullable=True,
    )
    relates_to_inbox_object: Mapped[Optional["InboxObject"]] = relationship(
        "InboxObject",
        foreign_keys=relates_to_inbox_object_id,
        remote_side=id,
        uselist=False,
    )
    relates_to_outbox_object_id = Column(
        Integer,
        ForeignKey("outbox.id"),
        nullable=True,
    )
    relates_to_outbox_object: Mapped[Optional["OutboxObject"]] = relationship(
        "OutboxObject",
        foreign_keys=[relates_to_outbox_object_id],
        uselist=False,
    )

    undone_by_inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=True)

    # Link the oubox AP ID to allow undo without any extra query
    liked_via_outbox_object_ap_id = Column(String, nullable=True)
    announced_via_outbox_object_ap_id = Column(String, nullable=True)

    is_bookmarked = Column(Boolean, nullable=False, default=False)

    # FIXME(ts): do we need this?
    has_replies = Column(Boolean, nullable=False, default=False)

    og_meta: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)

    @property
    def relates_to_anybox_object(self) -> Union["InboxObject", "OutboxObject"] | None:
        if self.relates_to_inbox_object_id:
            return self.relates_to_inbox_object
        elif self.relates_to_outbox_object_id:
            return self.relates_to_outbox_object
        else:
            return None

    @property
    def is_from_db(self) -> bool:
        return True

    @property
    def is_from_inbox(self) -> bool:
        return True


class OutboxObject(Base, BaseObject):
    __tablename__ = "outbox"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    is_hidden_from_homepage = Column(Boolean, nullable=False, default=False)

    public_id = Column(String, nullable=False, index=True)

    ap_type = Column(String, nullable=False, index=True)
    ap_id = Column(String, nullable=False, unique=True, index=True)
    ap_context = Column(String, nullable=True)
    ap_object: Mapped[ap.RawObject] = Column(JSON, nullable=False)

    activity_object_ap_id = Column(String, nullable=True, index=True)

    # Source content for activities (like Notes)
    source = Column(String, nullable=True)

    ap_published_at = Column(DateTime(timezone=True), nullable=False, default=now)
    visibility = Column(Enum(ap.VisibilityEnum), nullable=False)

    likes_count = Column(Integer, nullable=False, default=0)
    announces_count = Column(Integer, nullable=False, default=0)
    replies_count = Column(Integer, nullable=False, default=0)
    # reactions: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)

    webmentions = Column(JSON, nullable=True)

    og_meta: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)

    # For the featured collection
    is_pinned = Column(Boolean, nullable=False, default=False)

    # Never actually delete from the outbox
    is_deleted = Column(Boolean, nullable=False, default=False)

    # Used for Like, Announce and Undo activities
    relates_to_inbox_object_id = Column(
        Integer,
        ForeignKey("inbox.id"),
        nullable=True,
    )
    relates_to_inbox_object: Mapped[Optional["InboxObject"]] = relationship(
        "InboxObject",
        foreign_keys=[relates_to_inbox_object_id],
        uselist=False,
    )
    relates_to_outbox_object_id = Column(
        Integer,
        ForeignKey("outbox.id"),
        nullable=True,
    )
    relates_to_outbox_object: Mapped[Optional["OutboxObject"]] = relationship(
        "OutboxObject",
        foreign_keys=[relates_to_outbox_object_id],
        remote_side=id,
        uselist=False,
    )
    # For Follow activies
    relates_to_actor_id = Column(
        Integer,
        ForeignKey("actor.id"),
        nullable=True,
    )
    relates_to_actor: Mapped[Optional["Actor"]] = relationship(
        "Actor",
        foreign_keys=[relates_to_actor_id],
        uselist=False,
    )

    undone_by_outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=True)

    @property
    def actor(self) -> BaseActor:
        return LOCAL_ACTOR

    outbox_object_attachments: Mapped[list["OutboxObjectAttachment"]] = relationship(
        "OutboxObjectAttachment", uselist=True, backref="outbox_object"
    )

    @property
    def attachments(self) -> list[Attachment]:
        out = []
        for attachment in self.outbox_object_attachments:
            url = (
                BASE_URL
                + f"/attachments/{attachment.upload.content_hash}/{attachment.filename}"
            )
            out.append(
                Attachment.parse_obj(
                    {
                        "type": "Document",
                        "mediaType": attachment.upload.content_type,
                        "name": attachment.filename,
                        "url": url,
                        "proxiedUrl": url,
                        "resizedUrl": BASE_URL
                        + (
                            "/attachments/thumbnails/"
                            f"{attachment.upload.content_hash}"
                            f"/{attachment.filename}"
                        )
                        if attachment.upload.has_thumbnail
                        else None,
                    }
                )
            )
        return out

    @property
    def relates_to_anybox_object(self) -> Union["InboxObject", "OutboxObject"] | None:
        if self.relates_to_inbox_object_id:
            return self.relates_to_inbox_object
        elif self.relates_to_outbox_object_id:
            return self.relates_to_outbox_object
        else:
            return None

    @property
    def is_from_db(self) -> bool:
        return True

    @property
    def is_from_outbox(self) -> bool:
        return True


class Follower(Base):
    __tablename__ = "follower"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=False, unique=True)
    actor = relationship(Actor, uselist=False)

    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=False)
    inbox_object = relationship(InboxObject, uselist=False)

    ap_actor_id = Column(String, nullable=False, unique=True)


class Following(Base):
    __tablename__ = "following"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=False, unique=True)
    actor = relationship(Actor, uselist=False)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    ap_actor_id = Column(String, nullable=False, unique=True)


@enum.unique
class NotificationType(str, enum.Enum):
    NEW_FOLLOWER = "new_follower"
    UNFOLLOW = "unfollow"
    LIKE = "like"
    UNDO_LIKE = "undo_like"
    ANNOUNCE = "announce"
    UNDO_ANNOUNCE = "undo_announce"
    MENTION = "mention"


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    notification_type = Column(Enum(NotificationType), nullable=True)
    is_new = Column(Boolean, nullable=False, default=True)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=True)
    actor = relationship(Actor, uselist=False)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=True)
    outbox_object = relationship(OutboxObject, uselist=False)

    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=True)
    inbox_object = relationship(InboxObject, uselist=False)


class OutgoingActivity(Base):
    __tablename__ = "outgoing_activity"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    recipient = Column(String, nullable=False)
    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    tries = Column(Integer, nullable=False, default=0)
    next_try = Column(DateTime(timezone=True), nullable=True, default=now)

    last_try = Column(DateTime(timezone=True), nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_response = Column(String, nullable=True)

    is_sent = Column(Boolean, nullable=False, default=False)
    is_errored = Column(Boolean, nullable=False, default=False)
    error = Column(String, nullable=True)


class TaggedOutboxObject(Base):
    __tablename__ = "tagged_outbox_object"
    __table_args__ = (
        UniqueConstraint("outbox_object_id", "tag", name="uix_tagged_object"),
    )

    id = Column(Integer, primary_key=True, index=True)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    tag = Column(String, nullable=False, index=True)


class Upload(Base):
    __tablename__ = "upload"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    content_type: Mapped[str] = Column(String, nullable=False)
    content_hash = Column(String, nullable=False, unique=True)

    has_thumbnail = Column(Boolean, nullable=False)

    # Only set for images
    blurhash = Column(String, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image")


class OutboxObjectAttachment(Base):
    __tablename__ = "outbox_object_attachment"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    filename = Column(String, nullable=False)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)

    upload_id = Column(Integer, ForeignKey("upload.id"), nullable=False)
    upload = relationship(Upload, uselist=False)
