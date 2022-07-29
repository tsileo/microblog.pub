from copy import deepcopy

import httpx
import pytest
from respx import MockRouter

from app import activitypub as ap
from app import ldsig
from app.database import AsyncSession
from app.key import Key
from tests import factories

_SAMPLE_CREATE = {
    "type": "Create",
    "actor": "https://microblog.pub",
    "object": {
        "type": "Note",
        "sensitive": False,
        "cc": ["https://microblog.pub/followers"],
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "content": "<p>Hello world!</p>",
        "tag": [],
        "attributedTo": "https://microblog.pub",
        "published": "2018-05-21T15:51:59Z",
        "id": "https://microblog.pub/outbox/988179f13c78b3a7/activity",
        "url": "https://microblog.pub/note/988179f13c78b3a7",
    },
    "@context": ap.AS_EXTENDED_CTX,
    "published": "2018-05-21T15:51:59Z",
    "to": ["https://www.w3.org/ns/activitystreams#Public"],
    "cc": ["https://microblog.pub/followers"],
    "id": "https://microblog.pub/outbox/988179f13c78b3a7",
}


@pytest.mark.asyncio
async def test_linked_data_sig(
    async_db_session: AsyncSession,
    respx_mock: MockRouter,
) -> None:
    privkey, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://microblog.pub",
        username="dev",
        public_key=pubkey,
    )
    k = Key(ra.ap_id, f"{ra.ap_id}#main-key")
    k.load(privkey)
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    doc = deepcopy(_SAMPLE_CREATE)

    ldsig.generate_signature(doc, k)
    assert (await ldsig.verify_signature(async_db_session, doc)) is True
