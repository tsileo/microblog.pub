"""Implements HTTP signature for Flask requests.

Mastodon instances won't accept requests that are not signed using this scheme.

"""
import base64
import hashlib
import typing
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any
from typing import Dict
from typing import Optional

import fastapi
import httpx
from Crypto.Hash import SHA256
from Crypto.Signature import PKCS1_v1_5
from loguru import logger

from app import config
from app.key import Key
from app.key import get_key


def _build_signed_string(
    signed_headers: str, method: str, path: str, headers: Any, body_digest: str | None
) -> str:
    out = []
    for signed_header in signed_headers.split(" "):
        if signed_header == "(request-target)":
            out.append("(request-target): " + method.lower() + " " + path)
        elif signed_header == "digest" and body_digest:
            out.append("digest: " + body_digest)
        else:
            out.append(signed_header + ": " + headers[signed_header])
    return "\n".join(out)


def _parse_sig_header(val: Optional[str]) -> Optional[Dict[str, str]]:
    if not val:
        return None
    out = {}
    for data in val.split(","):
        k, v = data.split("=", 1)
        out[k] = v[1 : len(v) - 1]  # noqa: black conflict
    return out


def _verify_h(signed_string, signature, pubkey):
    signer = PKCS1_v1_5.new(pubkey)
    digest = SHA256.new()
    digest.update(signed_string.encode("utf-8"))
    return signer.verify(digest, signature)


def _body_digest(body: bytes) -> str:
    h = hashlib.new("sha256")
    h.update(body)  # type: ignore
    return "SHA-256=" + base64.b64encode(h.digest()).decode("utf-8")


@lru_cache(32)
def _get_public_key(key_id: str) -> Key:
    from app import activitypub as ap

    actor = ap.fetch(key_id)
    if actor["type"] == "Key":
        # The Key is not embedded in the Person
        k = Key(actor["owner"], actor["id"])
        k.load_pub(actor["publicKeyPem"])
    else:
        k = Key(actor["id"], actor["publicKey"]["id"])
        k.load_pub(actor["publicKey"]["publicKeyPem"])

    # Ensure the right key was fetch
    if key_id != k.key_id():
        raise ValueError(
            f"failed to fetch requested key {key_id}: got {actor['publicKey']['id']}"
        )

    return k


@dataclass(frozen=True)
class HTTPSigInfo:
    has_valid_signature: bool
    signed_by_ap_actor_id: str | None = None


async def httpsig_checker(
    request: fastapi.Request,
) -> HTTPSigInfo:
    body = await request.body()

    hsig = _parse_sig_header(request.headers.get("Signature"))
    if not hsig:
        logger.info("No HTTP signature found")
        return HTTPSigInfo(has_valid_signature=False)

    logger.debug(f"hsig={hsig}")
    signed_string = _build_signed_string(
        hsig["headers"],
        request.method,
        request.url.path,
        request.headers,
        _body_digest(body) if body else None,
    )

    try:
        k = _get_public_key(hsig["keyId"])
    except Exception:
        logger.exception(f'Failed to fetch HTTP sig key {hsig["keyId"]}')
        return HTTPSigInfo(has_valid_signature=False)

    httpsig_info = HTTPSigInfo(
        has_valid_signature=_verify_h(
            signed_string, base64.b64decode(hsig["signature"]), k.pubkey
        ),
        signed_by_ap_actor_id=k.owner,
    )
    logger.info(f"Valid HTTP signature for {httpsig_info.signed_by_ap_actor_id}")
    return httpsig_info


async def enforce_httpsig(
    request: fastapi.Request,
    httpsig_info: HTTPSigInfo = fastapi.Depends(httpsig_checker),
) -> HTTPSigInfo:
    if not httpsig_info.has_valid_signature:
        logger.warning(f"Invalid HTTP sig {httpsig_info=}")
        body = await request.body()
        logger.info(f"{body=}")
        raise fastapi.HTTPException(status_code=401, detail="Invalid HTTP sig")

    return httpsig_info


class HTTPXSigAuth(httpx.Auth):
    def __init__(self, key: Key) -> None:
        self.key = key

    def auth_flow(
        self, r: httpx.Request
    ) -> typing.Generator[httpx.Request, httpx.Response, None]:
        logger.info(f"keyid={self.key.key_id()}")

        bodydigest = None
        if r.content:
            bh = hashlib.new("sha256")
            bh.update(r.content)
            bodydigest = "SHA-256=" + base64.b64encode(bh.digest()).decode("utf-8")

        date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
        r.headers["Date"] = date
        if bodydigest:
            r.headers["Digest"] = bodydigest
            sigheaders = "(request-target) user-agent host date digest content-type"
        else:
            sigheaders = "(request-target) user-agent host date accept"

        to_be_signed = _build_signed_string(
            sigheaders, r.method, r.url.path, r.headers, bodydigest
        )
        if not self.key.privkey:
            raise ValueError("Should never happen")
        signer = PKCS1_v1_5.new(self.key.privkey)
        digest = SHA256.new()
        digest.update(to_be_signed.encode("utf-8"))
        sig = base64.b64encode(signer.sign(digest)).decode()

        key_id = self.key.key_id()
        sig_value = f'keyId="{key_id}",algorithm="rsa-sha256",headers="{sigheaders}",signature="{sig}"'  # noqa: E501
        logger.debug(f"signed request {sig_value=}")
        r.headers["Signature"] = sig_value
        yield r


k = Key(config.ID, f"{config.ID}#main-key")
k.load(get_key())
auth = HTTPXSigAuth(k)
