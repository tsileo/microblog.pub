import base64
import hashlib
import typing
from datetime import datetime

import pyld  # type: ignore
from Crypto.Hash import SHA256
from Crypto.Signature import PKCS1_v1_5
from loguru import logger
from pyld import jsonld  # type: ignore
from pyld.jsonld import JsonLdError  # type: ignore

from app import activitypub as ap
from app import config
from app.database import AsyncSession
from app.httpsig import _get_public_key
from app.utils import safe_http

if typing.TYPE_CHECKING:
    from app.key import Key


def _loader(url, options={}):
    """JSON-LD document loader.

    The (remote) contexts URLs come from untrusted documents: they are fetched
    with the SSRF protection (on every redirect hop) and a timeout.
    """
    # XXX: temp fix/hack is it seems to be down for now
    if url == "https://w3id.org/identity/v1":
        url = (
            "https://raw.githubusercontent.com/web-payments/web-payments.org"
            "/master/contexts/identity-v1.jsonld"
        )

    try:
        with safe_http.client(timeout=10.0) as client:
            resp = client.get(
                url,
                headers={
                    # See https://github.com/digitalbazaar/pyld/issues/133
                    "Accept": "application/ld+json",
                    "User-Agent": config.USER_AGENT,
                },
                follow_redirects=True,
            )
            resp.raise_for_status()
            return {
                "contentType": resp.headers.get("content-type")
                or "application/ld+json",
                "contextUrl": None,
                "documentUrl": str(resp.url),
                "document": resp.json(),
            }
    except Exception as cause:
        raise JsonLdError(
            "Could not retrieve a JSON-LD document from the URL.",
            "jsonld.LoadDocumentError",
            code="loading document failed",
            cause=cause,
        )


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

    # The signature must be made by the actor of the activity
    if "actor" not in doc or key.owner != ap.get_id(doc["actor"]):
        logger.warning(f"LD signature key {key_id} does not belong to the actor")
        return False

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
