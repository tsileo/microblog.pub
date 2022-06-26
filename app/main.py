import base64
import os
import sys
import time
from datetime import datetime
from io import BytesIO
from typing import Any
from typing import Type

import httpx
from dateutil.parser import isoparse
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Form
from fastapi import Request
from fastapi import Response
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse
from fastapi.responses import PlainTextResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from PIL import Image
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from app import activitypub as ap
from app import admin
from app import boxes
from app import config
from app import httpsig
from app import models
from app import templates
from app.actor import LOCAL_ACTOR
from app.actor import get_actors_metadata
from app.boxes import public_outbox_objects_count
from app.boxes import save_to_inbox
from app.config import BASE_URL
from app.config import DEBUG
from app.config import DOMAIN
from app.config import ID
from app.config import USER_AGENT
from app.config import USERNAME
from app.config import generate_csrf_token
from app.config import is_activitypub_requested
from app.config import verify_csrf_token
from app.database import get_db
from app.templates import is_current_user_admin
from app.uploads import UPLOAD_DIR
from app.webfinger import get_remote_follow_template

# TODO(ts):
#
# Next:
# - inbox/outbox admin
# - no counters anymore?
# - allow to show tags in the menu
# - support update post with history
# - inbox/outbox in the admin (as in show every objects)
# - show likes/announces counter for outbox activities
# - update actor support
# - hash config/profile to detect when to send Update actor
#
# - [ ] block support
# - [ ] make the media proxy authenticated
# - [ ] prevent SSRF (urlutils from little-boxes)
# - [ ] Dockerization
# - [ ] Webmentions
# - [ ] custom emoji
# - [ ] poll/questions support
# - [ ] cleanup tasks

app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(admin.router, prefix="/admin")
app.include_router(admin.unauthenticated_router, prefix="/admin")

logger.configure(extra={"request_id": "no_req_id"})
logger.remove()
logger_format = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "{extra[request_id]} - <level>{message}</level>"
)
logger.add(sys.stdout, format=logger_format)


@app.middleware("http")
async def request_middleware(request, call_next):
    start_time = time.perf_counter()
    request_id = os.urandom(8).hex()
    with logger.contextualize(request_id=request_id):
        logger.info(
            f"{request.client.host}:{request.client.port} - "
            f"{request.method} {request.url}"
        )
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            response.headers["Server"] = "microblogpub"
            elapsed_time = time.perf_counter() - start_time
            logger.info(f"status_code={response.status_code} {elapsed_time=:.2f}s")
            return response
        except Exception:
            logger.exception("Request failed")
            raise


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["referrer-policy"] = "no-referrer, strict-origin-when-cross-origin"
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["x-xss-protection"] = "1; mode=block"
    response.headers["x-frame-options"] = "SAMEORIGIN"
    # TODO(ts): disallow inline CSS?
    if DEBUG:
        return response
    response.headers["content-security-policy"] = (
        "default-src 'self'" + " style-src 'self' 'unsafe-inline';"
    )
    if not DEBUG:
        response.headers[
            "strict-transport-security"
        ] = "max-age=63072000; includeSubdomains"
    return response


DEFAULT_CTX = COLLECTION_CTX = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    {
        # AS ext
        "Hashtag": "as:Hashtag",
        "sensitive": "as:sensitive",
        "manuallyApprovesFollowers": "as:manuallyApprovesFollowers",
        # toot
        "toot": "http://joinmastodon.org/ns#",
        # "featured": "toot:featured",
        # schema
        "schema": "http://schema.org#",
        "PropertyValue": "schema:PropertyValue",
        "value": "schema:value",
    },
]


class ActivityPubResponse(JSONResponse):
    media_type = "application/activity+json"


@app.get("/")
def index(
    request: Request,
    db: Session = Depends(get_db),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
    page: int | None = None,
) -> templates.TemplateResponse | ActivityPubResponse:
    if is_activitypub_requested(request):
        return ActivityPubResponse(LOCAL_ACTOR.ap_actor)

    page = page or 1
    q = db.query(models.OutboxObject).filter(
        models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        models.OutboxObject.is_deleted.is_(False),
        models.OutboxObject.is_hidden_from_homepage.is_(False),
    )
    total_count = q.count()
    page_size = 2
    page_offset = (page - 1) * page_size

    outbox_objects = (
        q.options(
            joinedload(models.OutboxObject.outbox_object_attachments).options(
                joinedload(models.OutboxObjectAttachment.upload)
            )
        )
        .order_by(models.OutboxObject.ap_published_at.desc())
        .offset(page_offset)
        .limit(page_size)
        .all()
    )

    return templates.render_template(
        db,
        request,
        "index.html",
        {
            "request": request,
            "objects": outbox_objects,
            "current_page": page,
            "has_next_page": page_offset + len(outbox_objects) < total_count,
            "has_previous_page": page > 1,
        },
    )


def _build_followx_collection(
    db: Session,
    model_cls: Type[models.Following | models.Follower],
    path: str,
    page: bool | None,
    next_cursor: str | None,
) -> ap.RawObject:
    total_items = db.query(model_cls).count()

    if not page and not next_cursor:
        return {
            "@context": ap.AS_CTX,
            "id": ID + path,
            "first": ID + path + "?page=true",
            "type": "OrderedCollection",
            "totalItems": total_items,
        }

    q = db.query(model_cls).order_by(model_cls.created_at.desc())  # type: ignore
    if next_cursor:
        q = q.filter(model_cls.created_at < _decode_cursor(next_cursor))  # type: ignore
    q = q.limit(20)

    items = [followx for followx in q.all()]
    next_cursor = None
    if (
        items
        and db.query(model_cls)
        .filter(model_cls.created_at < items[-1].created_at)
        .count()
        > 0
    ):
        next_cursor = _encode_cursor(items[-1].created_at)

    collection_page = {
        "@context": ap.AS_CTX,
        "id": (
            ID + path + "?page=true"
            if not next_cursor
            else ID + path + f"?next_cursor={next_cursor}"
        ),
        "partOf": ID + path,
        "type": "OrderedCollectionPage",
        "orderedItems": [item.ap_actor_id for item in items],
    }
    if next_cursor:
        collection_page["next"] = ID + path + f"?next_cursor={next_cursor}"

    return collection_page


def _encode_cursor(val: datetime) -> str:
    return base64.urlsafe_b64encode(val.isoformat().encode()).decode()


def _decode_cursor(cursor: str) -> datetime:
    return isoparse(base64.urlsafe_b64decode(cursor).decode())


@app.get("/followers")
def followers(
    request: Request,
    page: bool | None = None,
    next_cursor: str | None = None,
    prev_cursor: str | None = None,
    db: Session = Depends(get_db),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse | templates.TemplateResponse:
    if is_activitypub_requested(request):
        return ActivityPubResponse(
            _build_followx_collection(
                db=db,
                model_cls=models.Follower,
                path="/followers",
                page=page,
                next_cursor=next_cursor,
            )
        )

    followers = (
        db.query(models.Follower)
        .options(joinedload(models.Follower.actor))
        .order_by(models.Follower.created_at.desc())
        .limit(20)
        .all()
    )

    # TODO: support next_cursor/prev_cursor
    actors_metadata = {}
    if is_current_user_admin(request):
        actors_metadata = get_actors_metadata(
            db,
            [f.actor for f in followers],
        )

    return templates.render_template(
        db,
        request,
        "followers.html",
        {
            "followers": followers,
            "actors_metadata": actors_metadata,
        },
    )


@app.get("/following")
def following(
    request: Request,
    page: bool | None = None,
    next_cursor: str | None = None,
    prev_cursor: str | None = None,
    db: Session = Depends(get_db),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse | templates.TemplateResponse:
    if is_activitypub_requested(request):
        return ActivityPubResponse(
            _build_followx_collection(
                db=db,
                model_cls=models.Following,
                path="/following",
                page=page,
                next_cursor=next_cursor,
            )
        )

    q = (
        db.query(models.Following)
        .options(joinedload(models.Following.actor))
        .order_by(models.Following.created_at.desc())
        .limit(20)
    )
    following = q.all()

    # TODO: support next_cursor/prev_cursor
    actors_metadata = {}
    if is_current_user_admin(request):
        actors_metadata = get_actors_metadata(
            db,
            [f.actor for f in following],
        )

    return templates.render_template(
        db,
        request,
        "following.html",
        {
            "following": following,
            "actors_metadata": actors_metadata,
        },
    )


@app.get("/outbox")
def outbox(
    db: Session = Depends(get_db),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse:
    outbox_objects = (
        db.query(models.OutboxObject)
        .filter(
            models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
            models.OutboxObject.is_deleted.is_(False),
        )
        .order_by(models.OutboxObject.ap_published_at.desc())
        .limit(20)
        .all()
    )
    return ActivityPubResponse(
        {
            "@context": DEFAULT_CTX,
            "id": f"{ID}/outbox",
            "type": "OrderedCollection",
            "totalItems": len(outbox_objects),
            "orderedItems": [
                ap.remove_context(ap.wrap_object_if_needed(a.ap_object))
                for a in outbox_objects
            ],
        }
    )


@app.get("/featured")
def featured(
    db: Session = Depends(get_db),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse:
    outbox_objects = (
        db.query(models.OutboxObject)
        .filter(
            models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
            models.OutboxObject.is_deleted.is_(False),
            models.OutboxObject.is_pinned.is_(True),
        )
        .order_by(models.OutboxObject.ap_published_at.desc())
        .limit(5)
        .all()
    )
    return ActivityPubResponse(
        {
            "@context": DEFAULT_CTX,
            "id": f"{ID}/featured",
            "type": "OrderedCollection",
            "totalItems": len(outbox_objects),
            "orderedItems": [ap.remove_context(a.ap_object) for a in outbox_objects],
        }
    )


@app.get("/o/{public_id}")
def outbox_by_public_id(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse | templates.TemplateResponse:
    # TODO: ACL?
    maybe_object = (
        db.query(models.OutboxObject)
        .options(
            joinedload(models.OutboxObject.outbox_object_attachments).options(
                joinedload(models.OutboxObjectAttachment.upload)
            )
        )
        .filter(
            models.OutboxObject.public_id == public_id,
            models.OutboxObject.is_deleted.is_(False),
        )
        .one_or_none()
    )
    if not maybe_object:
        raise HTTPException(status_code=404)

    if is_activitypub_requested(request):
        return ActivityPubResponse(maybe_object.ap_object)

    replies_tree = boxes.get_replies_tree(db, maybe_object)

    return templates.render_template(
        db,
        request,
        "object.html",
        {
            "replies_tree": replies_tree,
            "outbox_object": maybe_object,
        },
    )


@app.get("/o/{public_id}/activity")
def outbox_activity_by_public_id(
    public_id: str,
    db: Session = Depends(get_db),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse:
    # TODO: ACL?
    maybe_object = (
        db.query(models.OutboxObject)
        .filter(
            models.OutboxObject.public_id == public_id,
            models.OutboxObject.is_deleted.is_(False),
        )
        .one_or_none()
    )
    if not maybe_object:
        raise HTTPException(status_code=404)

    return ActivityPubResponse(ap.wrap_object(maybe_object.ap_object))


@app.get("/t/{tag}")
def tag_by_name(
    tag: str,
    request: Request,
    db: Session = Depends(get_db),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse | templates.TemplateResponse:
    # TODO(ts): implement HTML version
    # if is_activitypub_requested(request):
    return ActivityPubResponse(
        {
            "@context": ap.AS_CTX,
            "id": BASE_URL + f"/t/{tag}",
            "type": "OrderedCollection",
            "totalItems": 0,
            "orderedItems": [],
        }
    )


@app.post("/inbox")
async def inbox(
    request: Request,
    db: Session = Depends(get_db),
    httpsig_info: httpsig.HTTPSigInfo = Depends(httpsig.enforce_httpsig),
) -> Response:
    logger.info(f"headers={request.headers}")
    payload = await request.json()
    logger.info(f"{payload=}")
    save_to_inbox(db, payload)
    return Response(status_code=204)


@app.get("/remote_follow")
def get_remote_follow(
    request: Request,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    return templates.render_template(
        db,
        request,
        "remote_follow.html",
        {"remote_follow_csrf_token": generate_csrf_token()},
    )


@app.post("/remote_follow")
def post_remote_follow(
    request: Request,
    db: Session = Depends(get_db),
    csrf_check: None = Depends(verify_csrf_token),
    profile: str = Form(),
) -> RedirectResponse:
    if not profile.startswith("@"):
        profile = f"@{profile}"

    remote_follow_template = get_remote_follow_template(profile)
    if not remote_follow_template:
        raise HTTPException(status_code=404)

    return RedirectResponse(
        remote_follow_template.format(uri=ID),
        status_code=302,
    )


@app.get("/.well-known/webfinger")
def wellknown_webfinger(resource: str) -> JSONResponse:
    """Exposes/servers WebFinger data."""
    omg = f"acct:{USERNAME}@{DOMAIN}"
    logger.info(f"{resource == omg}/{resource}/{omg}/{len(resource)}/{len(omg)}")
    if resource not in [f"acct:{USERNAME}@{DOMAIN}", ID]:
        logger.info(f"Got invalid req for {resource}")
        raise HTTPException(status_code=404)

    out = {
        "subject": f"acct:{USERNAME}@{DOMAIN}",
        "aliases": [ID],
        "links": [
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": ID,
            },
            {"rel": "self", "type": "application/activity+json", "href": ID},
            {
                "rel": "http://ostatus.org/schema/1.0/subscribe",
                "template": BASE_URL + "/admin/lookup?query={uri}",
            },
        ],
    }

    return JSONResponse(out, media_type="application/jrd+json; charset=utf-8")


@app.get("/.well-known/nodeinfo")
async def well_known_nodeinfo() -> dict[str, Any]:
    return {
        "links": [
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                "href": f"{BASE_URL}/nodeinfo",
            }
        ]
    }


@app.get("/nodeinfo")
def nodeinfo(
    db: Session = Depends(get_db),
):
    local_posts = public_outbox_objects_count(db)
    return JSONResponse(
        {
            "version": "2.1",
            "software": {
                "name": "microblogpub",
                "version": config.VERSION,
                "repository": "https://github.com/tsileo/microblog.pub",
            },
            "protocols": ["activitypub"],
            "services": {"inbound": [], "outbound": []},
            "openRegistrations": False,
            "usage": {"users": {"total": 1}, "localPosts": local_posts},
            "metadata": {
                "nodeName": LOCAL_ACTOR.handle,
            },
        },
        media_type=(
            "application/json; "
            "profile=http://nodeinfo.diaspora.software/ns/schema/2.1#"
        ),
    )


proxy_client = httpx.AsyncClient()


@app.get("/proxy/media/{encoded_url}")
async def serve_proxy_media(request: Request, encoded_url: str) -> StreamingResponse:
    # Decode the base64-encoded URL
    url = base64.urlsafe_b64decode(encoded_url).decode()
    # Request the URL (and filter request headers)
    proxy_req = proxy_client.build_request(
        request.method,
        url,
        headers=[
            (k, v)
            for (k, v) in request.headers.raw
            if k.lower()
            not in [b"host", b"cookie", b"x-forwarded-for", b"x-real-ip", b"user-agent"]
        ]
        + [(b"user-agent", USER_AGENT.encode())],
    )
    proxy_resp = await proxy_client.send(proxy_req, stream=True)
    # Filter the headers
    proxy_resp_headers = [
        (k, v)
        for (k, v) in proxy_resp.headers.items()
        if k.lower()
        in [
            "content-length",
            "content-type",
            "content-range",
            "accept-ranges" "etag",
            "cache-control",
            "expires",
            "date",
            "last-modified",
        ]
    ]
    return StreamingResponse(
        proxy_resp.aiter_raw(),
        status_code=proxy_resp.status_code,
        headers=dict(proxy_resp_headers),
        background=BackgroundTask(proxy_resp.aclose),
    )


@app.get("/proxy/media/{encoded_url}/{size}")
def serve_proxy_media_resized(
    request: Request,
    encoded_url: str,
    size: int,
) -> PlainTextResponse:
    if size not in {50, 740}:
        raise ValueError("Unsupported size")

    # Decode the base64-encoded URL
    url = base64.urlsafe_b64decode(encoded_url).decode()
    # Request the URL (and filter request headers)
    proxy_resp = httpx.get(
        url,
        headers=[
            (k, v)
            for (k, v) in request.headers.raw
            if k.lower()
            not in [b"host", b"cookie", b"x-forwarded-for", b"x-real-ip", b"user-agent"]
        ]
        + [(b"user-agent", USER_AGENT.encode())],
    )
    if proxy_resp.status_code != 200:
        return PlainTextResponse(
            proxy_resp.content,
            status_code=proxy_resp.status_code,
        )

    # Filter the headers
    proxy_resp_headers = {
        k: v
        for (k, v) in proxy_resp.headers.items()
        if k.lower()
        in [
            "content-type",
            "etag",
            "cache-control",
            "expires",
            "last-modified",
        ]
    }

    try:
        out = BytesIO(proxy_resp.content)
        i = Image.open(out)
        if i.is_animated:
            raise ValueError
        i.thumbnail((size, size))
        resized_buf = BytesIO()
        i.save(resized_buf, format=i.format)
        resized_buf.seek(0)
        return PlainTextResponse(
            resized_buf.read(),
            media_type=i.get_format_mimetype(),  # type: ignore
            headers=proxy_resp_headers,
        )
    except ValueError:
        return PlainTextResponse(
            proxy_resp.content,
            headers=proxy_resp_headers,
        )
    except Exception:
        logger.exception(f"Failed to resize {url} on the fly")
        return PlainTextResponse(
            proxy_resp.content,
            headers=proxy_resp_headers,
        )


@app.get("/attachments/{content_hash}/{filename}")
def serve_attachment(
    content_hash: str,
    filename: str,
    db: Session = Depends(get_db),
):
    upload = (
        db.query(models.Upload)
        .filter(
            models.Upload.content_hash == content_hash,
        )
        .one_or_none()
    )
    if not upload:
        raise HTTPException(status_code=404)

    return FileResponse(
        UPLOAD_DIR / content_hash,
        media_type=upload.content_type,
    )


@app.get("/attachments/thumbnails/{content_hash}/{filename}")
def serve_attachment_thumbnail(
    content_hash: str,
    filename: str,
    db: Session = Depends(get_db),
):
    upload = (
        db.query(models.Upload)
        .filter(
            models.Upload.content_hash == content_hash,
        )
        .one_or_none()
    )
    if not upload or not upload.has_thumbnail:
        raise HTTPException(status_code=404)

    return FileResponse(
        UPLOAD_DIR / (content_hash + "_resized"),
        media_type=upload.content_type,
    )


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_file():
    return """User-agent: *
Disallow: /followers
Disallow: /following
Disallow: /admin"""
