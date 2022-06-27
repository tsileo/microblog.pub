from fastapi.testclient import TestClient

from app import activitypub as ap
from app import models
from app.config import generate_csrf_token
from app.database import Session
from app.utils.emoji import EMOJIS_BY_NAME
from tests.utils import generate_admin_session_cookies


def test_emoji_are_loaded() -> None:
    assert len(EMOJIS_BY_NAME) >= 1


def test_emoji_ap_endpoint(db: Session, client: TestClient) -> None:
    response = client.get("/e/goose_hacker", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    emoji_resp = response.json()
    assert emoji_resp["type"] == "Emoji"


def test_emoji_ap_endpoint__not_found(db: Session, client: TestClient) -> None:
    response = client.get("/e/goose_hacker2", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 404


def test_emoji_note_with_emoji(db: Session, client: TestClient) -> None:
    # Call admin endpoint to create a note with
    note_content = "😺 :goose_hacker:"

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
    assert emoji_tag["type"] == "Emoji"
    assert emoji_tag["name"] == ":goose_hacker:"
    url = emoji_tag["icon"]["url"]

    # And the custom emoji is rendered in the HTML version
    html_resp = client.get("/o/" + outbox_object.public_id)
    html_resp.raise_for_status()
    assert html_resp.status_code == 200
    assert url in html_resp.text
    # And the unicode emoji is rendered with twemoji
    assert f'/static/twemoji/{hex(ord("😺"))[2:]}.svg' in html_resp.text
