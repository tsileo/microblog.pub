from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import activitypub as ap
from app import models
from app.config import generate_csrf_token
from tests.utils import generate_admin_session_cookies


def test_tags__no_tags(
    db: Session,
    client: TestClient,
) -> None:
    response = client.get("/t/nope", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 404


def test_tags__note_with_tag(db: Session, client: TestClient) -> None:
    # Call admin endpoint to create a note with
    note_content = "Hello #testing"

    response = client.post(
        "/admin/actions/new",
        data={
            "redirect_url": "http://testserver/",
            "content": note_content,
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
    assert len(outbox_object.tags) == 1
    emoji_tag = outbox_object.tags[0]
    assert emoji_tag["type"] == "Hashtag"
    assert emoji_tag["name"] == "#testing"

    # And the tag page returns this note
    html_resp = client.get("/t/testing")
    html_resp.raise_for_status()
    assert html_resp.status_code == 200
    assert "Hello" in html_resp.text

    # And the AP version of the page turns the note too
    ap_resp = client.get("/t/testing", headers={"Accept": ap.AP_CONTENT_TYPE})
    ap_resp.raise_for_status()
    ap_json_resp = ap_resp.json()
    assert ap_json_resp["totalItems"] == 1
    assert ap_json_resp["orderedItems"] == [outbox_object.ap_id]
