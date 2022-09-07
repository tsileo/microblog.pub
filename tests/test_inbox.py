from unittest import mock
from uuid import uuid4

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import activitypub as ap
from app import models
from app.actor import LOCAL_ACTOR
from app.ap_object import RemoteObject
from tests import factories
from tests.utils import mock_httpsig_checker
from tests.utils import run_process_next_incoming_activity
from tests.utils import setup_inbox_delete
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower
from tests.utils import setup_remote_actor_as_following


def test_inbox_requires_httpsig(
    client: TestClient,
):
    response = client.post(
        "/inbox",
        headers={"Content-Type": ap.AS_CTX},
        json={},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid HTTP sig"


def test_inbox_incoming_follow_request(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    # When receiving a Follow activity
    follow_activity = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra,
            for_remote_actor=LOCAL_ACTOR,
        ),
        ra,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=follow_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # And the actor was saved in DB
    saved_actor = db.execute(select(models.Actor)).scalar_one()
    assert saved_actor.ap_id == ra.ap_id

    # And the Follow activity was saved in the inbox
    inbox_object = db.execute(select(models.InboxObject)).scalar_one()
    assert inbox_object.ap_object == follow_activity.ap_object

    # And a follower was internally created
    follower = db.execute(select(models.Follower)).scalar_one()
    assert follower.ap_actor_id == ra.ap_id
    assert follower.actor_id == saved_actor.id
    assert follower.inbox_object_id == inbox_object.id

    # And an Accept activity was created in the outbox
    outbox_object = db.execute(select(models.OutboxObject)).scalar_one()
    assert outbox_object.ap_type == "Accept"
    assert outbox_object.activity_object_ap_id == follow_activity.ap_id

    # And an outgoing activity was created to track the Accept activity delivery
    outgoing_activity = db.execute(select(models.OutgoingActivity)).scalar_one()
    assert outgoing_activity.outbox_object_id == outbox_object.id


def test_inbox_incoming_follow_request__manually_approves_followers(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    # When receiving a Follow activity
    follow_activity = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra,
            for_remote_actor=LOCAL_ACTOR,
        ),
        ra,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=follow_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    with mock.patch("app.boxes.MANUALLY_APPROVES_FOLLOWERS", True):
        run_process_next_incoming_activity()

    # And the actor was saved in DB
    saved_actor = db.execute(select(models.Actor)).scalar_one()
    assert saved_actor.ap_id == ra.ap_id

    # And the Follow activity was saved in the inbox
    inbox_object = db.execute(select(models.InboxObject)).scalar_one()
    assert inbox_object.ap_object == follow_activity.ap_object

    # And no follower was internally created
    assert db.scalar(select(func.count(models.Follower.id))) == 0


def test_inbox_accept_follow_request(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)
    actor_in_db = factories.ActorFactory.from_remote_actor(ra)

    # And a Follow activity in the outbox
    follow_id = uuid4().hex
    follow_from_outbox = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=LOCAL_ACTOR,
            for_remote_actor=ra,
            outbox_public_id=follow_id,
        ),
        LOCAL_ACTOR,
    )
    outbox_object = factories.OutboxObjectFactory.from_remote_object(
        follow_id, follow_from_outbox
    )

    # When receiving a Accept activity
    accept_activity = RemoteObject(
        factories.build_accept_activity(
            from_remote_actor=ra,
            for_remote_object=follow_from_outbox,
        ),
        ra,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=accept_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # And the Accept activity was saved in the inbox
    inbox_activity = db.execute(select(models.InboxObject)).scalar_one()
    assert inbox_activity.ap_type == "Accept"
    assert inbox_activity.relates_to_outbox_object_id == outbox_object.id
    assert inbox_activity.actor_id == actor_in_db.id

    # And a following entry was created internally
    following = db.execute(select(models.Following)).scalar_one()
    assert following.ap_actor_id == actor_in_db.ap_id


def test_inbox__create_from_follower(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Who is also a follower
    setup_remote_actor_as_follower(ra)

    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id=str(uuid4()),
            content="Hello",
            to=[LOCAL_ACTOR.ap_id],
        )
    )

    # When receiving a Create activity
    ro = RemoteObject(create_activity, ra)

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    # And when processing the incoming activity
    run_process_next_incoming_activity()

    # Then the Create activity was saved
    create_activity_from_inbox: models.InboxObject | None = db.execute(
        select(models.InboxObject).where(models.InboxObject.ap_type == "Create")
    ).scalar_one_or_none()
    assert create_activity_from_inbox
    assert create_activity_from_inbox.ap_id == ro.ap_id

    # And the Note object was created
    note_activity_from_inbox: models.InboxObject | None = db.execute(
        select(models.InboxObject).where(models.InboxObject.ap_type == "Note")
    ).scalar_one_or_none()
    assert note_activity_from_inbox
    assert note_activity_from_inbox.ap_id == ro.activity_object_ap_id


def test_inbox__create_already_deleted_object(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Who is also a follower
    follower = setup_remote_actor_as_follower(ra)

    # And a Create activity for a Note object
    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id=str(uuid4()),
            content="Hello",
            to=[LOCAL_ACTOR.ap_id],
        )
    )
    ro = RemoteObject(create_activity, ra)

    # And a Delete activity received for the create object
    setup_inbox_delete(follower.actor, ro.activity_object_ap_id)  # type: ignore

    # When receiving a Create activity
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    # And when processing the incoming activity
    run_process_next_incoming_activity()

    # Then the Create activity was saved
    create_activity_from_inbox: models.InboxObject | None = db.execute(
        select(models.InboxObject).where(models.InboxObject.ap_type == "Create")
    ).scalar_one_or_none()
    assert create_activity_from_inbox
    assert create_activity_from_inbox.ap_id == ro.ap_id
    # But it has the deleted flag
    assert create_activity_from_inbox.is_deleted is True

    # And the Note wasn't created
    assert (
        db.execute(
            select(models.InboxObject).where(models.InboxObject.ap_type == "Note")
        ).scalar_one_or_none()
        is None
    )


def test_inbox__actor_is_blocked(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Who is also a follower
    follower = setup_remote_actor_as_follower(ra)
    follower.actor.is_blocked = True
    db.commit()

    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id=str(uuid4()),
            content="Hello",
            to=[LOCAL_ACTOR.ap_id],
        )
    )

    # When receiving a Create activity
    ro = RemoteObject(create_activity, ra)

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    # And when processing the incoming activity from a blocked actor
    run_process_next_incoming_activity()

    # Then the Create activity was discarded
    assert (
        db.scalar(
            select(func.count(models.InboxObject.id)).where(
                models.InboxObject.ap_type != "Follow"
            )
        )
        == 0
    )


def test_inbox__move_activity(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Which is followed by the local actor
    following = setup_remote_actor_as_following(ra)
    old_actor = following.actor
    assert old_actor
    assert following.outbox_object
    follow_id = following.outbox_object.ap_id

    # When receiving a Move activity
    new_ra = setup_remote_actor(
        respx_mock,
        base_url="https://new-account.com",
        also_known_as=[ra.ap_id],
    )
    move_activity = RemoteObject(
        factories.build_move_activity(ra, new_ra),
        ra,
    )

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=move_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # And the Move activity was saved in the inbox
    inbox_activity = db.execute(select(models.InboxObject)).scalar_one()
    assert inbox_activity.ap_type == "Move"
    assert inbox_activity.actor_id == old_actor.id

    # And the following actor was deleted
    assert db.scalar(select(func.count(models.Following.id))) == 0

    # And the follow was undone
    assert (
        db.scalar(
            select(func.count(models.OutboxObject.id)).where(
                models.OutboxObject.ap_type == "Undo",
                models.OutboxObject.activity_object_ap_id == follow_id,
            )
        )
        == 1
    )

    # And the new account was followed
    assert (
        db.scalar(
            select(func.count(models.OutboxObject.id)).where(
                models.OutboxObject.ap_type == "Follow",
                models.OutboxObject.activity_object_ap_id == new_ra.ap_id,
            )
        )
        == 1
    )

    # And a notification was created
    notif = db.execute(
        select(models.Notification).where(
            models.Notification.notification_type == models.NotificationType.MOVE
        )
    ).scalar_one()
    assert notif.actor.ap_id == new_ra.ap_id
    assert notif.inbox_object_id == inbox_activity.id
