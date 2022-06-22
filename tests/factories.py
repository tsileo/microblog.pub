from uuid import uuid4

import factory  # type: ignore
from Crypto.PublicKey import RSA
from sqlalchemy import orm

from app import activitypub as ap
from app import actor
from app import models
from app.actor import RemoteActor
from app.ap_object import RemoteObject
from app.database import engine

_Session = orm.scoped_session(orm.sessionmaker(bind=engine))


def generate_key() -> tuple[str, str]:
    k = RSA.generate(1024)
    return k.exportKey("PEM").decode(), k.publickey().exportKey("PEM").decode()


def build_follow_activity(
    from_remote_actor: actor.RemoteActor,
    for_remote_actor: actor.RemoteActor,
    outbox_public_id: str | None = None,
) -> ap.RawObject:
    return {
        "@context": ap.AS_CTX,
        "type": "Follow",
        "id": from_remote_actor.ap_id + "/follow/" + (outbox_public_id or uuid4().hex),
        "actor": from_remote_actor.ap_id,
        "object": for_remote_actor.ap_id,
    }


def build_accept_activity(
    from_remote_actor: actor.RemoteActor,
    for_remote_object: RemoteObject,
    outbox_public_id: str | None = None,
) -> ap.RawObject:
    return {
        "@context": ap.AS_CTX,
        "type": "Accept",
        "id": from_remote_actor.ap_id + "/accept/" + (outbox_public_id or uuid4().hex),
        "actor": from_remote_actor.ap_id,
        "object": for_remote_object.ap_id,
    }


class BaseModelMeta:
    sqlalchemy_session = _Session
    sqlalchemy_session_persistence = "commit"


class RemoteActorFactory(factory.Factory):
    class Meta:
        model = RemoteActor
        exclude = (
            "base_url",
            "username",
            "public_key",
        )

    class Params:
        icon_url = None
        summary = "I like unit tests"

    ap_actor = factory.LazyAttribute(
        lambda o: {
            "@context": ap.AS_CTX,
            "type": "Person",
            "id": o.base_url,
            "following": o.base_url + "/following",
            "followers": o.base_url + "/followers",
            # "featured": ID + "/featured",
            "inbox": o.base_url + "/inbox",
            "outbox": o.base_url + "/outbox",
            "preferredUsername": o.username,
            "name": o.username,
            "summary": o.summary,
            "endpoints": {},
            "url": o.base_url,
            "manuallyApprovesFollowers": False,
            "attachment": [],
            "icon": {},
            "publicKey": {
                "id": f"{o.base_url}#main-key",
                "owner": o.base_url,
                "publicKeyPem": o.public_key,
            },
        }
    )


class ActorFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta(BaseModelMeta):
        model = models.Actor

    # ap_actor
    # ap_id
    ap_type = "Person"

    @classmethod
    def from_remote_actor(cls, ra):
        return cls(
            ap_type=ra.ap_type,
            ap_actor=ra.ap_actor,
            ap_id=ra.ap_id,
        )


class OutboxObjectFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta(BaseModelMeta):
        model = models.OutboxObject

    # public_id
    # relates_to_inbox_object_id
    # relates_to_outbox_object_id

    @classmethod
    def from_remote_object(cls, public_id, ro):
        return cls(
            public_id=public_id,
            ap_type=ro.ap_type,
            ap_id=ro.ap_id,
            ap_context=ro.context,
            ap_object=ro.ap_object,
            visibility=ro.visibility,
            og_meta=ro.og_meta,
            activity_object_ap_id=ro.activity_object_ap_id,
            is_hidden_from_homepage=True if ro.in_reply_to else False,
        )


class OutgoingActivityFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta(BaseModelMeta):
        model = models.OutgoingActivity

    # recipient
    # outbox_object_id
