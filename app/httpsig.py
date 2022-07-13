"""Implements HTTP signature for Flask requests.

Mastodon instances won't accept requests that are not signed using this scheme.

"""
import base64
import hashlib
import typing
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Dict
from typing import MutableMapping
from typing import Optional

import fastapi
import httpx
from cachetools import LFUCache
from Crypto.Hash import SHA256
from Crypto.Signature import PKCS1_v1_5
from loguru import logger
from sqlalchemy import select

from app import activitypub as ap
from app import config
from app.config import KEY_PATH
from app.database import AsyncSession
from app.database import get_db_session
from app.key import Key

_KEY_CACHE: MutableMapping[str, Key] = LFUCache(256)


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


async def _get_public_key(db_session: AsyncSession, key_id: str) -> Key:
    if cached_key := _KEY_CACHE.get(key_id):
        return cached_key

    # Check if the key belongs to an actor already in DB
    from app import models

    existing_actor = (
        await db_session.scalars(
            select(models.Actor).where(models.Actor.ap_id == key_id.split("#")[0])
        )
    ).one_or_none()
    if existing_actor and existing_actor.public_key_id == key_id:
        k = Key(existing_actor.ap_id, key_id)
        k.load_pub(existing_actor.public_key_as_pem)
        logger.info(f"Found {key_id} on an existing actor")
        _KEY_CACHE[key_id] = k
        return k

    # Fetch it
    from app import activitypub as ap

    actor = await ap.fetch(key_id)
    if actor["type"] == "Key":
        # The Key is not embedded in the Person
        k = Key(actor["owner"], actor["id"])
        k.load_pub(actor["publicKeyPem"])
    else:
        k = Key(actor["id"], actor["publicKey"]["id"])
        k.load_pub(actor["publicKey"]["publicKeyPem"])

    # Ensure the right key was fetch
    if key_id not in [k.key_id(), k.owner]:
        raise ValueError(
            f"failed to fetch requested key {key_id}: got {actor['publicKey']}"
        )

    _KEY_CACHE[key_id] = k
    return k


@dataclass(frozen=True)
class HTTPSigInfo:
    has_valid_signature: bool
    signed_by_ap_actor_id: str | None = None
    is_ap_actor_gone: bool = False


async def httpsig_checker(
    request: fastapi.Request,
    db_session: AsyncSession = fastapi.Depends(get_db_session),
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
        k = await _get_public_key(db_session, hsig["keyId"])
    except (ap.ObjectIsGoneError, ap.ObjectNotFoundError):
        logger.info("Actor is gone or not found")
        return HTTPSigInfo(has_valid_signature=False, is_ap_actor_gone=True)
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

        # Special case for Mastoodon instance that keep resending Delete
        # activities for actor we don't know about if we raise a 401
        if httpsig_info.is_ap_actor_gone:
            logger.info("Let's make Mastodon happy, returning a 202")
            raise fastapi.HTTPException(status_code=202)

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
k.load(KEY_PATH.read_text())
auth = HTTPXSigAuth(k)
