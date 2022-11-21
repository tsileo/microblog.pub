import base64
import hashlib
import json
import typing
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Dict
from typing import MutableMapping
from typing import Optional
from urllib.parse import urlparse

import fastapi
import httpx
from cachetools import LFUCache
from Crypto.Hash import SHA256
from Crypto.Signature import PKCS1_v1_5
from dateutil.parser import parse
from loguru import logger
from sqlalchemy import select

from app import activitypub as ap
from app import config
from app.config import BLOCKED_SERVERS
from app.config import KEY_PATH
from app.database import AsyncSession
from app.database import get_db_session
from app.key import Key
from app.utils.datetime import now

_KEY_CACHE: MutableMapping[str, Key] = LFUCache(256)


def _build_signed_string(
    signed_headers: str,
    method: str,
    path: str,
    headers: Any,
    body_digest: str | None,
    sig_data: dict[str, Any],
) -> tuple[str, datetime | None]:
    signature_date: datetime | None = None
    out = []
    for signed_header in signed_headers.split(" "):
        if signed_header == "(created)":
            signature_date = datetime.fromtimestamp(int(sig_data["created"])).replace(
                tzinfo=timezone.utc
            )
        elif signed_header == "date":
            signature_date = parse(headers["date"])

        if signed_header == "(request-target)":
            out.append("(request-target): " + method.lower() + " " + path)
        elif signed_header == "digest" and body_digest:
            out.append("digest: " + body_digest)
        elif signed_header in ["(created)", "(expires)"]:
            out.append(
                signed_header
                + ": "
                + sig_data[signed_header[1 : len(signed_header) - 1]]
            )
        else:
            out.append(signed_header + ": " + headers[signed_header])
    return "\n".join(out), signature_date


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


async def _get_public_key(
    db_session: AsyncSession,
    key_id: str,
    should_skip_cache: bool = False,
) -> Key:
    if not should_skip_cache and (cached_key := _KEY_CACHE.get(key_id)):
        logger.info(f"Key {key_id} found in cache")
        return cached_key

    # Check if the key belongs to an actor already in DB
    from app import models

    existing_actor = (
        await db_session.scalars(
            select(models.Actor).where(models.Actor.ap_id == key_id.split("#")[0])
        )
    ).one_or_none()
    if not should_skip_cache:
        if existing_actor and existing_actor.public_key_id == key_id:
            k = Key(existing_actor.ap_id, key_id)
            k.load_pub(existing_actor.public_key_as_pem)
            logger.info(f"Found {key_id} on an existing actor")
            _KEY_CACHE[key_id] = k
            return k

    # Fetch it
    from app import activitypub as ap
    from app.actor import RemoteActor
    from app.actor import update_actor_if_needed

    # Without signing the request as if it's the first contact, the 2 servers
    # might race to fetch each other key
    try:
        actor = await ap.fetch(key_id, disable_httpsig=True)
    except ap.ObjectUnavailableError:
        actor = await ap.fetch(key_id, disable_httpsig=False)

    if actor["type"] == "Key":
        # The Key is not embedded in the Person
        k = Key(actor["owner"], actor["id"])
        k.load_pub(actor["publicKeyPem"])
    else:
        k = Key(actor["id"], actor["publicKey"]["id"])
        k.load_pub(actor["publicKey"]["publicKeyPem"])

    # Ensure the right key was fetch
    # TODO: some server have the key ID `http://` but fetching it return `https`
    if key_id not in [k.key_id(), k.owner]:
        raise ValueError(
            f"failed to fetch requested key {key_id}: got {actor['publicKey']}"
        )

    if should_skip_cache and actor["type"] != "Key" and existing_actor:
        # We had to skip the cache, which means the actor key probably changed
        # and we want to update our cached version
        await update_actor_if_needed(db_session, existing_actor, RemoteActor(actor))
        await db_session.commit()

    _KEY_CACHE[key_id] = k
    return k


@dataclass(frozen=True)
class HTTPSigInfo:
    has_valid_signature: bool
    signed_by_ap_actor_id: str | None = None

    is_ap_actor_gone: bool = False
    is_unsupported_algorithm: bool = False
    is_expired: bool = False
    is_from_blocked_server: bool = False

    server: str | None = None


async def httpsig_checker(
    request: fastapi.Request,
    db_session: AsyncSession = fastapi.Depends(get_db_session),
) -> HTTPSigInfo:
    body = await request.body()

    hsig = _parse_sig_header(request.headers.get("Signature"))
    if not hsig:
        logger.info("No HTTP signature found")
        return HTTPSigInfo(has_valid_signature=False)

    try:
        key_id = hsig["keyId"]
    except KeyError:
        logger.info("Missing keyId")
        return HTTPSigInfo(
            has_valid_signature=False,
        )

    server = urlparse(key_id).hostname
    if server in BLOCKED_SERVERS:
        return HTTPSigInfo(
            has_valid_signature=False,
            server=server,
            is_from_blocked_server=True,
        )

    if alg := hsig.get("algorithm") not in ["rsa-sha256", "hs2019"]:
        logger.info(f"Unsupported HTTP sig algorithm: {alg}")
        return HTTPSigInfo(
            has_valid_signature=False,
            is_unsupported_algorithm=True,
            server=server,
        )

    # Try to drop Delete activity spams early on, this prevent making an extra
    # HTTP requests trying to fetch an unavailable actor to verify the HTTP sig
    try:
        if request.method == "POST" and request.url.path.endswith("/inbox"):
            from app import models  # TODO: solve this circular import

            activity = json.loads(body)
            actor_id = ap.get_id(activity["actor"])
            if (
                ap.as_list(activity["type"])[0] == "Delete"
                and actor_id == ap.get_id(activity["object"])
                and not (
                    await db_session.scalars(
                        select(models.Actor).where(
                            models.Actor.ap_id == actor_id,
                        )
                    )
                ).one_or_none()
            ):
                logger.info(f"Dropping Delete activity early for {body=}")
                raise fastapi.HTTPException(status_code=202)
    except fastapi.HTTPException as http_exc:
        raise http_exc
    except Exception:
        logger.exception("Failed to check for Delete spam")

    # logger.debug(f"hsig={hsig}")
    signed_string, signature_date = _build_signed_string(
        hsig["headers"],
        request.method,
        request.url.path,
        request.headers,
        _body_digest(body) if body else None,
        hsig,
    )

    # Sanity checks on the signature date
    if signature_date is None or now() - signature_date > timedelta(hours=12):
        logger.info(f"Signature expired: {signature_date=}")
        return HTTPSigInfo(
            has_valid_signature=False,
            is_expired=True,
            server=server,
        )

    try:
        k = await _get_public_key(db_session, hsig["keyId"])
    except (ap.ObjectIsGoneError, ap.ObjectNotFoundError):
        logger.info("Actor is gone or not found")
        return HTTPSigInfo(has_valid_signature=False, is_ap_actor_gone=True)
    except Exception:
        logger.exception(f'Failed to fetch HTTP sig key {hsig["keyId"]}')
        return HTTPSigInfo(has_valid_signature=False)

    has_valid_signature = _verify_h(
        signed_string, base64.b64decode(hsig["signature"]), k.pubkey
    )

    # If the signature is not valid, we may have to update the cached actor
    if not has_valid_signature:
        logger.info("Invalid signature, trying to refresh actor")
        try:
            k = await _get_public_key(db_session, hsig["keyId"], should_skip_cache=True)
            has_valid_signature = _verify_h(
                signed_string, base64.b64decode(hsig["signature"]), k.pubkey
            )
        except Exception:
            logger.exception("Failed to refresh actor")

    httpsig_info = HTTPSigInfo(
        has_valid_signature=has_valid_signature,
        signed_by_ap_actor_id=k.owner,
        server=server,
    )
    logger.info(f"Valid HTTP signature for {httpsig_info.signed_by_ap_actor_id}")
    return httpsig_info


async def enforce_httpsig(
    request: fastapi.Request,
    httpsig_info: HTTPSigInfo = fastapi.Depends(httpsig_checker),
) -> HTTPSigInfo:
    """FastAPI Depends"""
    if httpsig_info.is_from_blocked_server:
        logger.warning(f"{httpsig_info.server} is blocked")
        raise fastapi.HTTPException(status_code=403, detail="Blocked")

    if not httpsig_info.has_valid_signature:
        logger.warning(f"Invalid HTTP sig {httpsig_info=}")
        body = await request.body()
        logger.info(f"{body=}")

        # Special case for Mastoodon instance that keep resending Delete
        # activities for actor we don't know about if we raise a 401
        if httpsig_info.is_ap_actor_gone:
            logger.info("Let's make Mastodon happy, returning a 202")
            raise fastapi.HTTPException(status_code=202)

        detail = "Invalid HTTP sig"
        if httpsig_info.is_unsupported_algorithm:
            detail = "Unsupported signature algorithm, must be rsa-sha256 or hs2019"
        elif httpsig_info.is_expired:
            detail = "Signature expired"

        raise fastapi.HTTPException(status_code=401, detail=detail)

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

        to_be_signed, _ = _build_signed_string(
            sigheaders, r.method, r.url.path, r.headers, bodydigest, {}
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
