import httpx
import respx

from app import models
from app.actor import fetch_actor
from app.database import Session
from tests import factories


def test_fetch_actor(db: Session, respx_mock) -> None:
    # Given a remote actor
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    # When fetching this actor for the first time
    saved_actor = fetch_actor(db, ra.ap_id)

    # Then it has been fetched and saved in DB
    assert respx.calls.call_count == 1
    assert db.query(models.Actor).one().ap_id == saved_actor.ap_id

    # When fetching it a second time
    actor_from_db = fetch_actor(db, ra.ap_id)

    # Then it's read from the DB
    assert actor_from_db.ap_id == ra.ap_id
    assert db.query(models.Actor).count() == 1
    assert respx.calls.call_count == 1


def test_sqlalchemy_factory(db: Session) -> None:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    actor_in_db = factories.ActorFactory(
        ap_type=ra.ap_type,
        ap_actor=ra.ap_actor,
        ap_id=ra.ap_id,
    )
    assert actor_in_db.id == db.query(models.Actor).one().id
