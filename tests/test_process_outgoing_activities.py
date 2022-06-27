from uuid import uuid4

import httpx
import respx
from fastapi.testclient import TestClient

from app import models
from app.actor import LOCAL_ACTOR
from app.ap_object import RemoteObject
from app.database import Session
from app.outgoing_activities import _MAX_RETRIES
from app.outgoing_activities import new_outgoing_activity
from app.outgoing_activities import process_next_outgoing_activity
from tests import factories


def _setup_outbox_object() -> models.OutboxObject:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )

    # And a Follow activity in the outbox
    follow_id = uuid4().hex
    follow_from_outbox = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=LOCAL_ACTOR,
            for_remote_actor=ra,
            outbox_public_id=follow_id,
        )
    )
    outbox_object = factories.OutboxObjectFactory.from_remote_object(
        follow_id, follow_from_outbox
    )
    return outbox_object


def test_new_outgoing_activity(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()
    inbox_url = "https://example.com/inbox"

    if not outbox_object.id:
        raise ValueError("Should never happen")

    # When queuing the activity
    outgoing_activity = new_outgoing_activity(db, inbox_url, outbox_object.id)

    assert db.query(models.OutgoingActivity).one() == outgoing_activity
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == inbox_url


def test_process_next_outgoing_activity__no_next_activity(
    db: Session,
    respx_mock: respx.MockRouter,
) -> None:
    assert process_next_outgoing_activity(db) is False


def test_process_next_outgoing_activity__server_200(
    db: Session,
    respx_mock: respx.MockRouter,
) -> None:
    # And an outgoing activity
    outbox_object = _setup_outbox_object()

    recipient_inbox_url = "https://example.com/users/toto/inbox"
    respx_mock.post(recipient_inbox_url).mock(return_value=httpx.Response(204))

    outgoing_activity = factories.OutgoingActivityFactory(
        recipient=recipient_inbox_url,
        outbox_object_id=outbox_object.id,
    )

    # When processing the next outgoing activity
    # Then it is processed
    assert process_next_outgoing_activity(db) is True

    assert respx_mock.calls.call_count == 1

    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.is_sent is True
    assert outgoing_activity.last_status_code == 204
    assert outgoing_activity.error is None
    assert outgoing_activity.is_errored is False


def test_process_next_outgoing_activity__error_500(
    db: Session,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()
    recipient_inbox_url = "https://example.com/inbox"
    respx_mock.post(recipient_inbox_url).mock(
        return_value=httpx.Response(500, text="oops")
    )

    # And an outgoing activity
    outgoing_activity = factories.OutgoingActivityFactory(
        recipient=recipient_inbox_url,
        outbox_object_id=outbox_object.id,
    )

    # When processing the next outgoing activity
    # Then it is processed
    assert process_next_outgoing_activity(db) is True

    assert respx_mock.calls.call_count == 1

    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.is_sent is False
    assert outgoing_activity.last_status_code == 500
    assert outgoing_activity.last_response == "oops"
    assert outgoing_activity.is_errored is False
    assert outgoing_activity.tries == 1


def test_process_next_outgoing_activity__errored(
    db: Session,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()
    recipient_inbox_url = "https://example.com/inbox"
    respx_mock.post(recipient_inbox_url).mock(
        return_value=httpx.Response(500, text="oops")
    )

    # And an outgoing activity
    outgoing_activity = factories.OutgoingActivityFactory(
        recipient=recipient_inbox_url,
        outbox_object_id=outbox_object.id,
        tries=_MAX_RETRIES - 1,
    )

    # When processing the next outgoing activity
    # Then it is processed
    assert process_next_outgoing_activity(db) is True

    assert respx_mock.calls.call_count == 1

    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.is_sent is False
    assert outgoing_activity.last_status_code == 500
    assert outgoing_activity.last_response == "oops"
    assert outgoing_activity.is_errored is True

    # And it is skipped from processing
    assert process_next_outgoing_activity(db) is False


def test_process_next_outgoing_activity__connect_error(
    db: Session,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()
    recipient_inbox_url = "https://example.com/inbox"
    respx_mock.post(recipient_inbox_url).mock(side_effect=httpx.ConnectError)

    # And an outgoing activity
    outgoing_activity = factories.OutgoingActivityFactory(
        recipient=recipient_inbox_url,
        outbox_object_id=outbox_object.id,
    )

    # When processing the next outgoing activity
    # Then it is processed
    assert process_next_outgoing_activity(db) is True

    assert respx_mock.calls.call_count == 1

    outgoing_activity = db.query(models.OutgoingActivity).one()
    assert outgoing_activity.is_sent is False
    assert outgoing_activity.error is not None
    assert outgoing_activity.tries == 1


# TODO(ts):
# - parse retry after
