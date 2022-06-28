from copy import deepcopy

import pytest

from app import activitypub as ap
from app import ldsig
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


@pytest.mark.skip(reason="Working but slow")
def test_linked_data_sig():
    privkey, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://microblog.pub",
        username="dev",
        public_key=pubkey,
    )
    k = Key(ra.ap_id, f"{ra.ap_id}#main-key")
    k.load(privkey)

    doc = deepcopy(_SAMPLE_CREATE)

    ldsig.generate_signature(doc, k)
    assert ldsig.verify_signature(doc, k)
