import httpx
import pytest
import respx
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.actor import fetch_actor
from app.database import AsyncSession
from tests import factories


@pytest.mark.asyncio
async def test_fetch_actor(async_db_session: AsyncSession, respx_mock) -> None:
    # Given a remote actor
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    respx_mock.get(
        "https://example.com/.well-known/webfinger",
        params={"resource": "acct%3Atoto%40example.com"},
    ).mock(return_value=httpx.Response(200, json={"subject": "acct:toto@example.com"}))

    # When fetching this actor for the first time
    saved_actor = await fetch_actor(async_db_session, ra.ap_id)

    # Then it has been fetched and saved in DB
    assert respx.calls.call_count == 2
    assert (
        await async_db_session.execute(select(models.Actor))
    ).scalar_one().ap_id == saved_actor.ap_id

    # When fetching it a second time
    actor_from_db = await fetch_actor(async_db_session, ra.ap_id)

    # Then it's read from the DB
    assert actor_from_db.ap_id == ra.ap_id
    assert (
        await async_db_session.execute(select(func.count(models.Actor.id)))
    ).scalar_one() == 1
    assert respx.calls.call_count == 2


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
    assert actor_in_db.id == db.execute(select(models.Actor)).scalar_one().id
