from typing import Any

import fastapi
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import activitypub as ap
from app import httpsig
from app.database import AsyncSession
from app.httpsig import _KEY_CACHE
from app.httpsig import HTTPSigInfo
from app.key import Key
from tests import factories

_test_app = fastapi.FastAPI()


def _httpsig_info_to_dict(httpsig_info: HTTPSigInfo) -> dict[str, Any]:
    return {
        "has_valid_signature": httpsig_info.has_valid_signature,
        "signed_by_ap_actor_id": httpsig_info.signed_by_ap_actor_id,
    }


@_test_app.get("/httpsig_checker")
def get_httpsig_checker(
    httpsig_info: httpsig.HTTPSigInfo = fastapi.Depends(httpsig.httpsig_checker),
):
    return _httpsig_info_to_dict(httpsig_info)


@_test_app.post("/enforce_httpsig")
async def post_enforce_httpsig(
    request: fastapi.Request,
    httpsig_info: httpsig.HTTPSigInfo = fastapi.Depends(httpsig.enforce_httpsig),
):
    await request.json()
    return _httpsig_info_to_dict(httpsig_info)


def test_enforce_httpsig__no_signature(
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    with TestClient(_test_app) as client:
        response = client.post(
            "/enforce_httpsig",
            headers={"Content-Type": ap.AS_CTX},
            json={"enforce_httpsig": True},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid HTTP sig"


@pytest.mark.asyncio
async def test_enforce_httpsig__with_valid_signature(
    respx_mock: respx.MockRouter,
    async_db_session: AsyncSession,
) -> None:
    # Given a remote actor
    privkey, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key=pubkey,
    )
    k = Key(ra.ap_id, f"{ra.ap_id}#main-key")
    k.load(privkey)
    auth = httpsig.HTTPXSigAuth(k)
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    _KEY_CACHE.clear()

    async with httpx.AsyncClient(app=_test_app, base_url="http://test") as client:
        response = await client.post(
            "/enforce_httpsig",
            headers={"Content-Type": ap.AS_CTX},
            json={"enforce_httpsig": True},
            auth=auth,  # type: ignore
        )
        assert response.status_code == 200

    json_response = response.json()

    assert json_response["has_valid_signature"] is True
    assert json_response["signed_by_ap_actor_id"] == ra.ap_id


def test_httpsig_checker__no_signature(
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    with TestClient(_test_app) as client:
        response = client.get(
            "/httpsig_checker",
            headers={"Accept": ap.AS_CTX},
        )

    assert response.status_code == 200
    json_response = response.json()
    assert json_response["has_valid_signature"] is False
    assert json_response["signed_by_ap_actor_id"] is None


@pytest.mark.asyncio
async def test_httpsig_checker__with_valid_signature(
    respx_mock: respx.MockRouter,
    async_db_session: AsyncSession,
) -> None:
    # Given a remote actor
    privkey, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key=pubkey,
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    k = Key(ra.ap_id, f"{ra.ap_id}#main-key")
    k.load(privkey)
    auth = httpsig.HTTPXSigAuth(k)

    _KEY_CACHE.clear()

    async with httpx.AsyncClient(app=_test_app, base_url="http://test") as client:
        response = await client.get(
            "/httpsig_checker",
            headers={"Accept": ap.AS_CTX},
            auth=auth,  # type: ignore
        )

        assert response.status_code == 200
        json_response = response.json()

    assert json_response["has_valid_signature"] is True
    assert json_response["signed_by_ap_actor_id"] == ra.ap_id


@pytest.mark.asyncio
async def test_httpsig_checker__with_invvalid_signature(
    respx_mock: respx.MockRouter,
    async_db_session: AsyncSession,
) -> None:
    # Given a remote actor
    privkey, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key=pubkey,
    )
    k = Key(ra.ap_id, f"{ra.ap_id}#main-key")
    k.load(privkey)
    auth = httpsig.HTTPXSigAuth(k)

    ra2_privkey, ra2_pubkey = factories.generate_key()
    ra2 = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key=ra2_pubkey,
    )
    assert ra.ap_id == ra2.ap_id
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra2.ap_actor))

    _KEY_CACHE.clear()

    async with httpx.AsyncClient(app=_test_app, base_url="http://test") as client:
        response = await client.get(
            "/httpsig_checker",
            headers={"Accept": ap.AS_CTX},
            auth=auth,  # type: ignore
        )

        assert response.status_code == 200
        json_response = response.json()

    assert json_response["has_valid_signature"] is False
    assert json_response["signed_by_ap_actor_id"] == ra.ap_id
