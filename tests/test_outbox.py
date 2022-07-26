from unittest import mock
from uuid import uuid4

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import activitypub as ap
from app import actor
from app import models
from app import webfinger
from app.actor import LOCAL_ACTOR
from app.ap_object import RemoteObject
from app.config import generate_csrf_token
from tests import factories
from tests.utils import generate_admin_session_cookies


def test_outbox__no_activities(
    db: Session,
    client: TestClient,
) -> None:
    response = client.get("/outbox", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 200

    json_response = response.json()
    assert json_response["totalItems"] == 0
    assert json_response["orderedItems"] == []


def _setup_remote_actor(respx_mock: respx.MockRouter) -> actor.RemoteActor:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    return ra


def _remote_actor_as_follower(ra: actor.RemoteActor) -> models.Follower:
    actor = factories.ActorFactory.from_remote_actor(ra)

    follow_id = uuid4().hex
    follow_from_inbox = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra,
            for_remote_actor=LOCAL_ACTOR,
            outbox_public_id=follow_id,
        ),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        follow_from_inbox, actor
    )

    follower = factories.FollowerFactory(
        inbox_object_id=inbox_object.id,
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
    )
    return follower


def test_send_follow_request(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = _setup_remote_actor(respx_mock)

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


def test_send_create_activity__no_followers_and_with_mention(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # given a remote actor
    ra = _setup_remote_actor(respx_mock)

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
    ra = _setup_remote_actor(respx_mock)

    # who is a follower
    follower = _remote_actor_as_follower(ra)

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
    ra = _setup_remote_actor(respx_mock)

    # who is a follower
    follower = _remote_actor_as_follower(ra)

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
    ra = _setup_remote_actor(respx_mock)

    # who is a follower
    follower = _remote_actor_as_follower(ra)

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
    ra = _setup_remote_actor(respx_mock)

    # who is a follower
    follower = _remote_actor_as_follower(ra)

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
