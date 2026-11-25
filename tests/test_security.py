"""Regression tests for security issues (identity spoofing, ACL, XSS, IndieAuth)."""
import base64
import hashlib
from datetime import datetime
from unittest import mock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import activitypub as ap
from app import httpsig
from app import ldsig
from app import media
from app import models
from app import templates
from app.actor import fetch_actor
from app.ap_object import RemoteObject
from app.boxes import save_to_inbox
from app.config import ID
from app.config import generate_csrf_token
from app.database import AsyncSession
from app.httpsig import _KEY_CACHE
from app.key import Key
from app.main import app
from app.utils import url as url_utils
from tests import factories
from tests.test_safe_http import ssrf_protection_enabled  # noqa: F401
from tests.utils import generate_admin_session_cookies
from tests.utils import setup_outbox_note
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower

ATTACKER_KEY_ID = "https://evil.example/key"


def _mock_webfinger(respx_mock: respx.MockRouter) -> None:
    respx_mock.route(path="/.well-known/webfinger").mock(
        return_value=httpx.Response(404)
    )


def _attacker_key_claiming(owner: str) -> tuple[Key, dict]:
    privkey, pubkey = factories.generate_key()
    k = Key(owner, ATTACKER_KEY_ID)
    k.load(privkey)
    return k, {
        "@context": ap.AS_EXTENDED_CTX,
        "type": "Key",
        "id": ATTACKER_KEY_ID,
        "owner": owner,
        "publicKeyPem": pubkey,
    }


# HTTP signatures / identity


@pytest.mark.asyncio
async def test_httpsig__key_owned_by_actor_on_another_host_is_rejected(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    victim = setup_remote_actor(respx_mock, base_url="https://victim.example")
    _mock_webfinger(respx_mock)
    attacker_key, key_doc = _attacker_key_claiming(owner=victim.ap_id)
    respx_mock.get(ATTACKER_KEY_ID).mock(return_value=httpx.Response(200, json=key_doc))
    _KEY_CACHE.clear()

    note = factories.build_note_object(victim, content="forged")
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=factories.build_create_activity(note),
            auth=httpsig.HTTPXSigAuth(attacker_key),  # type: ignore
        )

    assert resp.status_code == 401
    assert not (await async_db_session.scalars(select(models.IncomingActivity))).all()


def test_httpsig__invalid_signature_does_not_grant_followers_only_access(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    _, pubkey = factories.generate_key()
    follower = factories.RemoteActorFactory(
        base_url="https://follower.example", username="bob", public_key=pubkey
    )
    respx_mock.get(follower.ap_id).mock(
        return_value=httpx.Response(200, json=follower.ap_actor)
    )
    setup_remote_actor_as_follower(follower)
    note = setup_outbox_note(content="secret", to=[ID + "/followers"])
    assert note.visibility == ap.VisibilityEnum.FOLLOWERS_ONLY
    _KEY_CACHE.clear()

    date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    key_id = follower.ap_actor["publicKey"]["id"]
    resp = client.get(
        f"/o/{note.public_id}",
        headers={
            "Accept": ap.AP_CONTENT_TYPE,
            "Date": date,
            "Signature": (
                f'keyId="{key_id}",algorithm="rsa-sha256",'
                'headers="(request-target) date",signature="AAAA"'
            ),
        },
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_httpsig__post_without_signed_digest_is_rejected(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    privkey, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://example.com", username="toto", public_key=pubkey
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    k = Key(ra.ap_id, f"{ra.ap_id}#main-key")
    k.load(privkey)
    _KEY_CACHE.clear()

    class _NoDigestAuth(httpsig.HTTPXSigAuth):
        def auth_flow(self, r):  # type: ignore
            date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
            r.headers["Date"] = date
            sigheaders = "(request-target) host date"
            to_be_signed, _ = httpsig._build_signed_string(
                sigheaders, r.method, r.url.path, r.headers, None, {}
            )
            signer = httpsig.PKCS1_v1_5.new(self.key.privkey)
            digest = httpsig.SHA256.new()
            digest.update(to_be_signed.encode())
            sig = base64.b64encode(signer.sign(digest)).decode()
            r.headers["Signature"] = (
                f'keyId="{self.key.key_id()}",algorithm="rsa-sha256",'
                f'headers="{sigheaders}",signature="{sig}"'
            )
            yield r

    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json={"type": "Follow"},
            auth=_NoDigestAuth(k),  # type: ignore
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_fetch__object_id_on_another_host_is_rejected(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    forged = factories.RemoteActorFactory(
        base_url="https://victim.example", username="toto", public_key="pk"
    ).ap_actor
    respx_mock.get("https://evil.example/alias").mock(
        return_value=httpx.Response(200, json=forged)
    )
    _mock_webfinger(respx_mock)

    with pytest.raises(ap.InvalidObjectOriginError):
        await fetch_actor(async_db_session, "https://evil.example/alias")

    assert not (await async_db_session.scalars(select(models.Actor))).all()


@pytest.mark.asyncio
async def test_ldsig__signature_by_another_actor_is_rejected(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    privkey, pubkey = factories.generate_key()
    attacker = factories.RemoteActorFactory(
        base_url="https://evil.example", username="eve", public_key=pubkey
    )
    respx_mock.get(attacker.ap_id).mock(
        return_value=httpx.Response(200, json=attacker.ap_actor)
    )
    k = Key(attacker.ap_id, attacker.ap_actor["publicKey"]["id"])
    k.load(privkey)
    _KEY_CACHE.clear()

    # Skip the JSON-LD normalization (network) to focus on the key/actor check,
    # the signed string is then always "od"
    assert k.privkey
    digest = httpsig.SHA256.new()
    digest.update(b"od")
    signature_value = base64.b64encode(
        httpsig.PKCS1_v1_5.new(k.privkey).sign(digest)
    ).decode()
    # Signed by the attacker key, for an activity of another actor
    doc: ap.RawObject = {
        "@context": ap.AS_CTX,
        "type": "Create",
        "id": "https://victim.example/note/1/activity",
        "actor": "https://victim.example",
        "object": {"type": "Note", "id": "https://victim.example/note/1"},
        "signature": {
            "type": "RsaSignature2017",
            "creator": k.key_id(),
            "created": "2022-01-01T00:00:00Z",
            "signatureValue": signature_value,
        },
    }
    with mock.patch.object(ldsig, "_options_hash", lambda d: "o"), mock.patch.object(
        ldsig, "_doc_hash", lambda d: "d"
    ):
        assert (await ldsig.verify_signature(async_db_session, doc)) is False

        # Sanity check: the same signature is valid for the attacker activity
        assert (
            await ldsig.verify_signature(
                async_db_session, {**doc, "actor": attacker.ap_id}
            )
        ) is True


@pytest.mark.asyncio
async def test_save_to_inbox__activity_not_hosted_by_actor_is_dropped(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    _mock_webfinger(respx_mock)
    note = factories.build_note_object(ra)
    create = factories.build_create_activity(note)
    create["id"] = "https://victim.example/squatted/activity"

    await save_to_inbox(async_db_session, create, ra.ap_id)

    assert not (await async_db_session.scalars(select(models.InboxObject))).all()


def test_jsonld_loader__blocks_private_contexts(
    ssrf_protection_enabled: None,  # noqa: F811
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get("http://169.254.169.254/latest/meta-data/").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(ldsig.JsonLdError):
        ldsig._doc_hash(
            {"@context": "http://169.254.169.254/latest/meta-data/", "type": "Like"}
        )

    assert not route.called


# Media proxy / XSS


@pytest.mark.parametrize(
    "content_type,expected_content_type",
    [
        ("image/png", "image/png"),
        ("video/mp4", "video/mp4"),
        ("text/html", "application/octet-stream"),
        ("text/javascript", "application/octet-stream"),
        ("image/svg+xml", "application/octet-stream"),
        (None, "application/octet-stream"),
    ],
)
def test_proxy_media__only_serves_passive_content_types(
    client: TestClient,
    respx_mock: respx.MockRouter,
    content_type: str | None,
    expected_content_type: str,
) -> None:
    url = "https://evil.example/attachment"
    upstream_headers: dict[str, str] = {}
    if content_type:
        upstream_headers["content-type"] = content_type
    respx_mock.get(url).mock(
        return_value=httpx.Response(
            200,
            headers=upstream_headers,
            content=b"<script>alert(1)</script>",
        )
    )

    for path_suffix in ["", "/740"]:
        path = media.proxied_media_url(url).removeprefix(media.BASE_URL)
        resp = client.get(path + path_suffix)

        assert resp.status_code == 200
        assert resp.headers["content-type"] == expected_content_type
        assert resp.headers["content-security-policy"] == (
            "default-src 'none'; sandbox"
        )
        if expected_content_type == "application/octet-stream":
            assert resp.headers["content-disposition"] == "attachment"


def test_csrf_error__redirect_url_is_not_injected(client: TestClient) -> None:
    payload = '"><script src="/proxy/media/1/2/3"></script><a href="'
    resp = client.post(
        "/remote_follow",
        headers={"Accept": "text/html"},
        data={"csrf_token": "invalid", "profile": "x", "redirect_url": payload},
    )

    assert resp.status_code == 403
    assert '<script src="/proxy/media/1/2/3">' not in resp.text


def test_login__redirect_is_local_and_cookie_is_httponly(
    db: Session,
    client: TestClient,
) -> None:
    resp = client.post(
        "/admin/login",
        data={
            "csrf_token": generate_csrf_token(),
            "password": "toto",
            "redirect": "https://evil.example/",
        },
        allow_redirects=False,
    )

    assert resp.status_code == 302
    assert not resp.headers["location"].startswith("https://evil.example")
    assert "httponly" in resp.headers["set-cookie"].lower()


def test_templates__custom_emoji_name_cannot_inject_markup() -> None:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com", username="toto", public_key="pk"
    )
    name = 'x" onerror="alert(1)'
    obj = RemoteObject(
        {
            **factories.build_note_object(ra, content=f"<p>{name}</p>"),
            "tag": [
                {
                    "type": "Emoji",
                    "name": name,
                    "icon": {"url": "https://example.com/e.png"},
                }
            ],
        },
        ra,
    )

    html = templates._clean_html(obj.content, obj)  # type: ignore

    # The invalid emoji name is ignored (the text itself is harmless)
    assert "custom-emoji" not in html
    assert "<img" not in html


def test_templates__remote_object_on_lookalike_domain_is_not_local() -> None:
    ra = factories.RemoteActorFactory(
        base_url=ID + ".evil.example", username="toto", public_key="pk"
    )
    obj = RemoteObject(
        factories.build_note_object(
            ra, content='<p><a href="/x" onclick="alert(1)">x</a></p>'
        ),
        ra,
    )
    assert obj.ap_id.startswith(ID)

    html = templates._clean_html(obj.content, obj)  # type: ignore

    assert "onclick" not in html


# IndieAuth


def _setup_auth_request(
    db: Session,
    code: str = "code",
    code_challenge: str | None = None,
) -> models.IndieAuthAuthorizationRequest:
    auth_request = models.IndieAuthAuthorizationRequest(
        code=code,
        scope="create",
        redirect_uri="https://app.example/callback",
        client_id="https://app.example/",
        code_challenge=code_challenge,
        code_challenge_method="S256" if code_challenge else None,
    )
    db.add(auth_request)
    db.commit()
    return auth_request


def test_indieauth__pkce_code_verifier_is_checked(
    db: Session,
    client: TestClient,
) -> None:
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    _setup_auth_request(db, code="code1", code_challenge=challenge)
    _setup_auth_request(db, code="code2", code_challenge=challenge)
    form = {
        "grant_type": "authorization_code",
        "client_id": "https://app.example/",
        "redirect_uri": "https://app.example/callback",
    }

    bad = client.post("/token", data={**form, "code": "code1", "code_verifier": "x"})
    missing = client.post("/token", data={**form, "code": "code2"})
    _setup_auth_request(db, code="code3", code_challenge=challenge)
    good = client.post(
        "/token", data={**form, "code": "code3", "code_verifier": verifier}
    )

    assert bad.status_code == 400
    assert missing.status_code == 400
    assert good.status_code == 200
    assert good.json()["access_token"]


def test_indieauth__code_is_not_sent_to_foreign_redirect_uri(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("https://app.example/").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )

    resp = client.post(
        "/admin/indieauth",
        cookies=generate_admin_session_cookies(),
        data={
            "csrf_token": generate_csrf_token(),
            "redirect_uri": "https://evil.example/callback",
            "state": "state",
            "client_id": "https://app.example/",
            "code_challenge": "",
            "code_challenge_method": "",
            "scopes": "create",
        },
    )

    assert resp.status_code == 400
    assert not db.execute(select(models.IndieAuthAuthorizationRequest)).all()


def test_indieauth__refresh_token_of_revoked_token_is_rejected(
    db: Session,
    client: TestClient,
) -> None:
    auth_request = _setup_auth_request(db)
    db.add(
        models.IndieAuthAccessToken(
            indieauth_authorization_request_id=auth_request.id,
            access_token="access",
            refresh_token="refresh",
            expires_in=3600,
            scope="create",
            is_revoked=True,
        )
    )
    db.commit()

    with pytest.raises(ValueError):
        client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "https://app.example/",
                "refresh_token": "refresh",
            },
        )


def test_c2s_outbox__requires_create_scope(
    db: Session,
    client: TestClient,
) -> None:
    db.add(
        models.IndieAuthAccessToken(
            access_token="read-only",
            expires_in=3600,
            scope="read",
        )
    )
    db.commit()

    resp = client.post(
        "/outbox",
        headers={"Authorization": "Bearer read-only"},
        json={"type": "Create", "actor": ID, "object": {"type": "Note"}},
    )

    assert resp.status_code == 403


def test_is_same_host() -> None:
    assert url_utils.is_same_host("https://a.example/x", "http://a.example/y")
    assert not url_utils.is_same_host("https://a.example/x", "https://b.example/")
    assert not url_utils.is_same_host("https://a.example.evil/x", "https://a.example")
    assert not url_utils.is_same_host("/relative", "/relative")
