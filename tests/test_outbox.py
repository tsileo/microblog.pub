import httpx
import respx
from fastapi.testclient import TestClient

from app import models
from app.config import generate_csrf_token
from app.database import Session
from tests import factories
from tests.utils import generate_admin_session_cookies


def test_send_follow_request(
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
