from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import activitypub as ap
from app.actor import LOCAL_ACTOR

_ACCEPTED_AP_HEADERS = [
    "application/activity+json",
    "application/activity+json; charset=utf-8",
    "application/ld+json",
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
]


def test_index__html(db: Session, client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("accept", _ACCEPTED_AP_HEADERS)
def test_index__ap(db: Session, client: TestClient, accept: str):
    response = client.get("/", headers={"Accept": accept})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    assert response.json() == LOCAL_ACTOR.ap_actor


def test_followers__ap(client, db) -> None:
    response = client.get("/followers", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    json_resp = response.json()
    assert json_resp["id"].endswith("/followers")
    assert "first" in json_resp


def test_followers__ap_hides_followers(client, db) -> None:
    with mock.patch("app.main.config.HIDES_FOLLOWERS", True):
        response = client.get("/followers", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    json_resp = response.json()
    assert json_resp["id"].endswith("/followers")
    assert "first" not in json_resp


def test_followers__html(client, db) -> None:
    response = client.get("/followers")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_followers__html_hides_followers(client, db) -> None:
    with mock.patch("app.main.config.HIDES_FOLLOWERS", True):
        response = client.get("/followers", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


def test_following__ap(client, db) -> None:
    response = client.get("/following", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    json_resp = response.json()
    assert json_resp["id"].endswith("/following")
    assert "first" in json_resp


def test_following__ap_hides_following(client, db) -> None:
    with mock.patch("app.main.config.HIDES_FOLLOWING", True):
        response = client.get("/following", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    json_resp = response.json()
    assert json_resp["id"].endswith("/following")
    assert "first" not in json_resp


def test_following__html(client, db) -> None:
    response = client.get("/following")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_following__html_hides_following(client, db) -> None:
    with mock.patch("app.main.config.HIDES_FOLLOWING", True):
        response = client.get("/following", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
