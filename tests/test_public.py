import pytest
from fastapi.testclient import TestClient

from app.database import Session

_ACCEPTED_AP_HEADERS = [
    "application/activity+json",
    "application/activity+json; charset=utf-8",
    "application/ld+json",
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
]


@pytest.mark.anyio
def test_index(db: Session, client: TestClient):
    response = client.get("/")
    assert response.status_code == 200


@pytest.mark.parametrize("accept", _ACCEPTED_AP_HEADERS)
def test__ap_version(client, db, accept: str) -> None:
    response = client.get("/followers", headers={"Accept": accept})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/activity+json"
    assert response.json()["id"].endswith("/followers")


def test__html(client, db) -> None:
    response = client.get("/followers", headers={"Accept": "application/activity+json"})
    assert response.status_code == 200
