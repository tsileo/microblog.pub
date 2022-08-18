import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import activitypub as ap
from app import models
from app.ap_object import RemoteObject
from tests import factories
from tests.utils import mock_httpsig_checker
from tests.utils import run_process_next_incoming_activity
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_following_and_follower


def test_inbox__incoming_delete_for_unknown_actor(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor who is already deleted
    ra = factories.RemoteActorFactory(
        base_url="https://deleted.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(404, json=ra.ap_actor))

    # When receiving a Delete activity for an unknown actor
    delete_activity = RemoteObject(
        factories.build_delete_activity(
            from_remote_actor=ra,
            deleted_object_ap_id=ra.ap_id,
        ),
        ra,
    )
    with mock_httpsig_checker(ra, has_valid_signature=False, is_ap_actor_gone=True):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=delete_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    # And no incoming activity was created
    assert db.scalar(select(func.count(models.IncomingActivity.id))) == 0


def test_inbox__incoming_delete_for_known_actor(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Which is both followed and a follower
    following, _ = setup_remote_actor_as_following_and_follower(ra)
    actor = following.actor
    assert actor
    assert following.outbox_object

    # TODO: setup few more activities (like announce and create)

    # When receiving a Delete activity for an unknown actor
    delete_activity = RemoteObject(
        factories.build_delete_activity(
            from_remote_actor=ra,
            deleted_object_ap_id=ra.ap_id,
        ),
        ra,
    )

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=delete_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then every inbox object from the actor was deleted
    assert (
        db.scalar(
            select(func.count(models.InboxObject.id)).where(
                models.InboxObject.actor_id == actor.id,
                models.InboxObject.is_deleted.is_(False),
            )
        )
        == 0
    )

    # And the following actor was deleted
    assert db.scalar(select(func.count(models.Following.id))) == 0

    # And the follower actor was deleted too
    assert db.scalar(select(func.count(models.Follower.id))) == 0

    # And the actor was marked in deleted
    db.refresh(actor)
    assert actor.is_deleted is True
