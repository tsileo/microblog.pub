from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import activitypub as ap


def test_tags__no_tags(
    db: Session,
    client: TestClient,
) -> None:
    response = client.get("/t/nope", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 404
