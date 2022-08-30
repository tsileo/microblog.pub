from unittest import mock

import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import activitypub as ap
from app import models
from app import webfinger
from app.actor import LOCAL_ACTOR
from app.config import generate_csrf_token
from tests.utils import generate_admin_session_cookies
from tests.utils import setup_inbox_note
from tests.utils import setup_outbox_note
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower


def test_outbox__no_activities(
    db: Session,
    client: TestClient,
) -> None:
    response = client.get("/outbox", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 200

    json_response = response.json()
    assert json_response["totalItems"] == 0
    assert json_response["orderedItems"] == []


def test_send_follow_request(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = setup_remote_actor(respx_mock)

    response = client.post(
        "/admin/actions/follow",
        data={
            "redirect_url": "http://testserver/",
            "ap_actor_id": ra.ap_id,
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
    )

    # Then the server returns a 302
    assert response.status_code == 302
    assert response.headers.get("Location") == "http://testserver/"

    # And the Follow activity was created in the outbox
    outbox_object = db.query(models.OutboxObject).one()
    assert outbox_object.ap_type == "Follow"
    assert outbox_object.activity_object_ap_id == ra.ap_id

    # And an outgoing activity was queued
    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == ra.inbox_url


def test_send_delete__reverts_side_effects(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = setup_remote_actor(respx_mock)

    # who is a follower
    follower = setup_remote_actor_as_follower(ra)
    actor = follower.actor

    # with a note that has existing replies
    inbox_note = setup_inbox_note(actor)
    inbox_note.replies_count = 1
    db.commit()

    # and a local reply
    outbox_note = setup_outbox_note(
        to=[ap.AS_PUBLIC],
        cc=[LOCAL_ACTOR.followers_collection_id],  # type: ignore
        in_reply_to=inbox_note.ap_id,
    )
    inbox_note.replies_count = inbox_note.replies_count + 1
    db.commit()

    response = client.post(
        "/admin/actions/delete",
        data={
            "redirect_url": "http://testserver/",
            "ap_object_id": outbox_note.ap_id,
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
    )

    # Then the server returns a 302
    assert response.status_code == 302
    assert response.headers.get("Location") == "http://testserver/"

    # And the Delete activity was created in the outbox
    outbox_object = db.execute(
        select(models.OutboxObject).where(models.OutboxObject.ap_type == "Delete")
    ).scalar_one()
    assert outbox_object.ap_type == "Delete"
    assert outbox_object.activity_object_ap_id == outbox_note.ap_id

    # And an outgoing activity was queued
    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == ra.inbox_url

    # And the replies count of the replied object was decremented
    db.refresh(inbox_note)
    assert inbox_note.replies_count == 1


def test_send_create_activity__no_followers_and_with_mention(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = setup_remote_actor(respx_mock)

    with mock.patch.object(webfinger, "get_actor_url", return_value=ra.ap_id):
        response = client.post(
            "/admin/actions/new",
            data={
                "redirect_url": "http://testserver/",
                "content": "hi @toto@example.com",
                "visibility": ap.VisibilityEnum.PUBLIC.name,
                "csrf_token": generate_csrf_token(),
            },
            cookies=generate_admin_session_cookies(),
        )

    # Then the server returns a 302
    assert response.status_code == 302

    # And the Follow activity was created in the outbox
    outbox_object = db.query(models.OutboxObject).one()
    assert outbox_object.ap_type == "Note"

    # And an outgoing activity was queued
    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == ra.inbox_url


def test_send_create_activity__with_followers(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = setup_remote_actor(respx_mock)

    # who is a follower
    follower = setup_remote_actor_as_follower(ra)

    with mock.patch.object(webfinger, "get_actor_url", return_value=ra.ap_id):
        response = client.post(
            "/admin/actions/new",
            data={
                "redirect_url": "http://testserver/",
                "content": "hi followers",
                "visibility": ap.VisibilityEnum.PUBLIC.name,
                "csrf_token": generate_csrf_token(),
            },
            cookies=generate_admin_session_cookies(),
        )

    # Then the server returns a 302
    assert response.status_code == 302

    # And the Follow activity was created in the outbox
    outbox_object = db.query(models.OutboxObject).one()
    assert outbox_object.ap_type == "Note"

    # And an outgoing activity was queued
    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == follower.actor.inbox_url


def test_send_create_activity__question__one_of(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = setup_remote_actor(respx_mock)

    # who is a follower
    follower = setup_remote_actor_as_follower(ra)

    with mock.patch.object(webfinger, "get_actor_url", return_value=ra.ap_id):
        response = client.post(
            "/admin/actions/new",
            data={
                "redirect_url": "http://testserver/",
                "content": "hi followers",
                "visibility": ap.VisibilityEnum.PUBLIC.name,
                "csrf_token": generate_csrf_token(),
                "poll_type": "oneOf",
                "poll_duration": 5,
                "poll_answer_1": "A",
                "poll_answer_2": "B",
            },
            cookies=generate_admin_session_cookies(),
        )

    # Then the server returns a 302
    assert response.status_code == 302

    # And the Follow activity was created in the outbox
    outbox_object = db.query(models.OutboxObject).one()
    assert outbox_object.ap_type == "Question"
    assert outbox_object.is_one_of_poll is True
    assert len(outbox_object.poll_items) == 2
    assert {pi["name"] for pi in outbox_object.poll_items} == {"A", "B"}
    assert outbox_object.is_poll_ended is False

    # And an outgoing activity was queued
    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == follower.actor.inbox_url


def test_send_create_activity__question__any_of(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = setup_remote_actor(respx_mock)

    # who is a follower
    follower = setup_remote_actor_as_follower(ra)

    with mock.patch.object(webfinger, "get_actor_url", return_value=ra.ap_id):
        response = client.post(
            "/admin/actions/new",
            data={
                "redirect_url": "http://testserver/",
                "content": "hi followers",
                "visibility": ap.VisibilityEnum.PUBLIC.name,
                "csrf_token": generate_csrf_token(),
                "poll_type": "anyOf",
                "poll_duration": 10,
                "poll_answer_1": "A",
                "poll_answer_2": "B",
                "poll_answer_3": "C",
                "poll_answer_4": "D",
            },
            cookies=generate_admin_session_cookies(),
        )

    # Then the server returns a 302
    assert response.status_code == 302

    # And the Follow activity was created in the outbox
    outbox_object = db.query(models.OutboxObject).one()
    assert outbox_object.ap_type == "Question"
    assert outbox_object.is_one_of_poll is False
    assert len(outbox_object.poll_items) == 4
    assert {pi["name"] for pi in outbox_object.poll_items} == {"A", "B", "C", "D"}
    assert outbox_object.is_poll_ended is False

    # And an outgoing activity was queued
    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == follower.actor.inbox_url


def test_send_create_activity__article(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = setup_remote_actor(respx_mock)

    # who is a follower
    follower = setup_remote_actor_as_follower(ra)

    with mock.patch.object(webfinger, "get_actor_url", return_value=ra.ap_id):
        response = client.post(
            "/admin/actions/new",
            data={
                "redirect_url": "http://testserver/",
                "content": "hi followers",
                "visibility": ap.VisibilityEnum.PUBLIC.name,
                "csrf_token": generate_csrf_token(),
                "name": "Article",
            },
            cookies=generate_admin_session_cookies(),
        )

    # Then the server returns a 302
    assert response.status_code == 302

    # And the Follow activity was created in the outbox
    outbox_object = db.query(models.OutboxObject).one()
    assert outbox_object.ap_type == "Article"
    assert outbox_object.ap_object["name"] == "Article"

    # And an outgoing activity was queued
    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == follower.actor.inbox_url
