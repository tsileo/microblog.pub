import base64
import hashlib
import typing
from datetime import datetime

import pyld  # type: ignore
from Crypto.Hash import SHA256
from Crypto.Signature import PKCS1_v1_5
from loguru import logger
from pyld import jsonld  # type: ignore

from app import activitypub as ap
from app.database import AsyncSession
from app.httpsig import _get_public_key

if typing.TYPE_CHECKING:
    from app.key import Key


requests_loader = pyld.documentloader.requests.requests_document_loader()


def _loader(url, options={}):
    # See https://github.com/digitalbazaar/pyld/issues/133
    options["headers"]["Accept"] = "application/ld+json"

    # XXX: temp fix/hack is it seems to be down for now
    if url == "https://w3id.org/identity/v1":
        url = (
            "https://raw.githubusercontent.com/web-payments/web-payments.org"
            "/master/contexts/identity-v1.jsonld"
        )
    return requests_loader(url, options)


pyld.jsonld.set_document_loader(_loader)


def _options_hash(doc: ap.RawObject) -> str:
    doc = dict(doc["signature"])
    for k in ["type", "id", "signatureValue"]:
        if k in doc:
            del doc[k]
    doc["@context"] = "https://w3id.org/security/v1"
    normalized = jsonld.normalize(
        doc, {"algorithm": "URDNA2015", "format": "application/nquads"}
    )
    h = hashlib.new("sha256")
    h.update(normalized.encode("utf-8"))
    return h.hexdigest()


def _doc_hash(doc: ap.RawObject) -> str:
    doc = dict(doc)
    if "signature" in doc:
        del doc["signature"]
    normalized = jsonld.normalize(
        doc, {"algorithm": "URDNA2015", "format": "application/nquads"}
    )
    h = hashlib.new("sha256")
    h.update(normalized.encode("utf-8"))
    return h.hexdigest()


async def verify_signature(
    db_session: AsyncSession,
    doc: ap.RawObject,
) -> bool:
    if "signature" not in doc:
        logger.warning("The object does contain a signature")
        return False

    key_id = doc["signature"]["creator"]
    key = await _get_public_key(db_session, key_id)
    to_be_signed = _options_hash(doc) + _doc_hash(doc)
    signature = doc["signature"]["signatureValue"]
    signer = PKCS1_v1_5.new(key.pubkey or key.privkey)  # type: ignore
    digest = SHA256.new()
    digest.update(to_be_signed.encode("utf-8"))
    return signer.verify(digest, base64.b64decode(signature))  # type: ignore


def generate_signature(doc: ap.RawObject, key: "Key") -> None:
    options = {
        "type": "RsaSignature2017",
        "creator": doc["actor"] + "#main-key",
        "created": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    doc["signature"] = options
    to_be_signed = _options_hash(doc) + _doc_hash(doc)
    if not key.privkey:
        raise ValueError(f"missing privkey on key {key!r}")

    signer = PKCS1_v1_5.new(key.privkey)
    digest = SHA256.new()
    digest.update(to_be_signed.encode("utf-8"))
    sig = base64.b64encode(signer.sign(digest))  # type: ignore
    options["signatureValue"] = sig.decode("utf-8")
