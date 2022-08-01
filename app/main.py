import base64
import os
import sys
import time
from datetime import timezone
from io import BytesIO
from typing import Any
from typing import MutableMapping
from typing import Type

import httpx
import starlette
from asgiref.typing import ASGI3Application
from asgiref.typing import ASGIReceiveCallable
from asgiref.typing import ASGISendCallable
from asgiref.typing import Scope
from cachetools import LFUCache
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
from feedgen.feed import FeedGenerator  # type: ignore
from loguru import logger
from PIL import Image
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import Message
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # type: ignore

from app import activitypub as ap
from app import admin
from app import boxes
from app import config
from app import httpsig
from app import indieauth
from app import micropub
from app import models
from app import templates
from app import webmentions
from app.actor import LOCAL_ACTOR
from app.actor import get_actors_metadata
from app.boxes import public_outbox_objects_count
from app.config import BASE_URL
from app.config import DEBUG
from app.config import DOMAIN
from app.config import ID
from app.config import USER_AGENT
from app.config import USERNAME
from app.config import is_activitypub_requested
from app.config import verify_csrf_token
from app.database import AsyncSession
from app.database import get_db_session
from app.incoming_activities import new_ap_incoming_activity
from app.templates import is_current_user_admin
from app.uploads import UPLOAD_DIR
from app.utils import pagination
from app.utils.emoji import EMOJIS_BY_NAME
from app.utils.url import check_url
from app.webfinger import get_remote_follow_template

_RESIZED_CACHE: MutableMapping[tuple[str, int], tuple[bytes, str, Any]] = LFUCache(32)


# TODO(ts):
#
# Next:
# - allow to manually approve follow requests
# - prevent double accept/double follow
# - UI support for updating posts
# - indieauth tweaks
# - API for posting notes
# - allow to block servers
# - FT5 text search
# - support update post with history?
# - cleanup tasks


class CustomMiddleware:
    """Raw ASGI middleware as using starlette base middleware causes issues
    with both:
     - Jinja2: https://github.com/encode/starlette/issues/472
     - async SQLAchemy: https://github.com/tiangolo/fastapi/issues/4719
    """

    def __init__(
        self,
        app: ASGI3Application,
    ) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        # We only care about HTTP requests
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_details = {"status_code": None}
        start_time = time.perf_counter()
        request_id = os.urandom(8).hex()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":

                # Extract the HTTP response status code
                response_details["status_code"] = message["status"]

                # And add the security headers
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["Server"] = "microblogpub"
                headers[
                    "referrer-policy"
                ] = "no-referrer, strict-origin-when-cross-origin"
                headers["x-content-type-options"] = "nosniff"
                headers["x-xss-protection"] = "1; mode=block"
                headers["x-frame-options"] = "SAMEORIGIN"
                # TODO(ts): disallow inline CSS?
                headers[
                    "content-security-policy"
                ] = "default-src 'self'; style-src 'self' 'unsafe-inline';"
                if not DEBUG:
                    headers[
                        "strict-transport-security"
                    ] = "max-age=63072000; includeSubdomains"

            await send(message)  # type: ignore

        # Make loguru ouput the request ID on every log statement within
        # the request
        with logger.contextualize(request_id=request_id):
            client_host, client_port = scope["client"]  # type: ignore
            scheme = scope["scheme"]
            server_host, server_port = scope["server"]  # type: ignore
            request_method = scope["method"]
            request_path = scope["path"]
            headers = Headers(raw=scope["headers"])  # type: ignore
            user_agent = headers.get("user-agent")
            logger.info(
                f"{client_host}:{client_port} - "
                f"{request_method} "
                f"{scheme}://{server_host}:{server_port}{request_path} - "
                f'"{user_agent}"'
            )
            try:
                await self.app(scope, receive, send_wrapper)  # type: ignore
            finally:
                elapsed_time = time.perf_counter() - start_time
                logger.info(
                    f"status_code={response_details['status_code']} "
                    f"{elapsed_time=:.2f}s"
                )

        return None


app = FastAPI(docs_url=None, redoc_url=None)
app.mount(
    "/custom_emoji",
    StaticFiles(directory="data/custom_emoji"),
    name="custom_emoji",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(admin.router, prefix="/admin")
app.include_router(admin.unauthenticated_router, prefix="/admin")
app.include_router(indieauth.router)
app.include_router(micropub.router)
app.include_router(webmentions.router)

# XXX: order matters, the proxy middleware needs to be last
app.add_middleware(CustomMiddleware)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=config.CONFIG.trusted_hosts)

logger.configure(extra={"request_id": "no_req_id"})
logger.remove()
logger_format = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "{extra[request_id]} - <level>{message}</level>"
)
logger.add(sys.stdout, format=logger_format)


class ActivityPubResponse(JSONResponse):
    media_type = "application/activity+json"


@app.get("/")
async def index(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
    page: int | None = None,
) -> templates.TemplateResponse | ActivityPubResponse:
    if is_activitypub_requested(request):
        return ActivityPubResponse(LOCAL_ACTOR.ap_actor)

    page = page or 1
    where = (
        models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        models.OutboxObject.is_deleted.is_(False),
        models.OutboxObject.is_hidden_from_homepage.is_(False),
        models.OutboxObject.ap_type != "Article",
    )
    q = select(models.OutboxObject).where(*where)
    total_count = await db_session.scalar(
        select(func.count(models.OutboxObject.id)).where(*where)
    )
    page_size = 20
    page_offset = (page - 1) * page_size

    outbox_objects_result = await db_session.scalars(
        q.options(
            joinedload(models.OutboxObject.outbox_object_attachments).options(
                joinedload(models.OutboxObjectAttachment.upload)
            ),
            joinedload(models.OutboxObject.relates_to_inbox_object).options(
                joinedload(models.InboxObject.actor),
            ),
            joinedload(models.OutboxObject.relates_to_outbox_object).options(
                joinedload(models.OutboxObject.outbox_object_attachments).options(
                    joinedload(models.OutboxObjectAttachment.upload)
                ),
            ),
        )
        .order_by(models.OutboxObject.ap_published_at.desc())
        .offset(page_offset)
        .limit(page_size)
    )
    outbox_objects = outbox_objects_result.unique().all()

    return await templates.render_template(
        db_session,
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


@app.get("/articles")
async def articles(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
    page: int | None = None,
) -> templates.TemplateResponse | ActivityPubResponse:
    # TODO: special ActivityPub collection for Article

    where = (
        models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        models.OutboxObject.is_deleted.is_(False),
        models.OutboxObject.is_hidden_from_homepage.is_(False),
        models.OutboxObject.ap_type == "Article",
    )
    q = select(models.OutboxObject).where(*where)

    outbox_objects_result = await db_session.scalars(
        q.options(
            joinedload(models.OutboxObject.outbox_object_attachments).options(
                joinedload(models.OutboxObjectAttachment.upload)
            ),
            joinedload(models.OutboxObject.relates_to_inbox_object).options(
                joinedload(models.InboxObject.actor),
            ),
            joinedload(models.OutboxObject.relates_to_outbox_object).options(
                joinedload(models.OutboxObject.outbox_object_attachments).options(
                    joinedload(models.OutboxObjectAttachment.upload)
                ),
            ),
        ).order_by(models.OutboxObject.ap_published_at.desc())
    )
    outbox_objects = outbox_objects_result.unique().all()

    return await templates.render_template(
        db_session,
        request,
        "articles.html",
        {
            "request": request,
            "objects": outbox_objects,
        },
    )


async def _build_followx_collection(
    db_session: AsyncSession,
    model_cls: Type[models.Following | models.Follower],
    path: str,
    page: bool | None,
    next_cursor: str | None,
) -> ap.RawObject:
    total_items = await db_session.scalar(select(func.count(model_cls.id)))

    if not page and not next_cursor:
        return {
            "@context": ap.AS_CTX,
            "id": ID + path,
            "first": ID + path + "?page=true",
            "type": "OrderedCollection",
            "totalItems": total_items,
        }

    q = select(model_cls).order_by(model_cls.created_at.desc())  # type: ignore
    if next_cursor:
        q = q.where(
            model_cls.created_at < pagination.decode_cursor(next_cursor)  # type: ignore
        )
    q = q.limit(20)

    items = [followx for followx in (await db_session.scalars(q)).all()]
    next_cursor = None
    if (
        items
        and await db_session.scalar(
            select(func.count(model_cls.id)).where(
                model_cls.created_at < items[-1].created_at
            )
        )
        > 0
    ):
        next_cursor = pagination.encode_cursor(items[-1].created_at)

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


@app.get("/followers")
async def followers(
    request: Request,
    page: bool | None = None,
    next_cursor: str | None = None,
    prev_cursor: str | None = None,
    db_session: AsyncSession = Depends(get_db_session),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse | templates.TemplateResponse:
    if is_activitypub_requested(request):
        return ActivityPubResponse(
            await _build_followx_collection(
                db_session=db_session,
                model_cls=models.Follower,
                path="/followers",
                page=page,
                next_cursor=next_cursor,
            )
        )

    # We only show the most recent 20 followers on the public website
    followers_result = await db_session.scalars(
        select(models.Follower)
        .options(joinedload(models.Follower.actor))
        .order_by(models.Follower.created_at.desc())
        .limit(20)
    )
    followers = followers_result.unique().all()

    actors_metadata = {}
    if is_current_user_admin(request):
        actors_metadata = await get_actors_metadata(
            db_session,
            [f.actor for f in followers],
        )

    return await templates.render_template(
        db_session,
        request,
        "followers.html",
        {
            "followers": followers,
            "actors_metadata": actors_metadata,
        },
    )


@app.get("/following")
async def following(
    request: Request,
    page: bool | None = None,
    next_cursor: str | None = None,
    prev_cursor: str | None = None,
    db_session: AsyncSession = Depends(get_db_session),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse | templates.TemplateResponse:
    if is_activitypub_requested(request):
        return ActivityPubResponse(
            await _build_followx_collection(
                db_session=db_session,
                model_cls=models.Following,
                path="/following",
                page=page,
                next_cursor=next_cursor,
            )
        )

    # We only show the most recent 20 follows on the public website
    following = (
        (
            await db_session.scalars(
                select(models.Following)
                .options(joinedload(models.Following.actor))
                .order_by(models.Following.created_at.desc())
            )
        )
        .unique()
        .all()
    )

    actors_metadata = {}
    if is_current_user_admin(request):
        actors_metadata = await get_actors_metadata(
            db_session,
            [f.actor for f in following],
        )

    return await templates.render_template(
        db_session,
        request,
        "following.html",
        {
            "following": following,
            "actors_metadata": actors_metadata,
        },
    )


@app.get("/outbox")
async def outbox(
    db_session: AsyncSession = Depends(get_db_session),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse:
    # By design, we only show the last 20 public activities in the oubox
    outbox_objects = (
        await db_session.scalars(
            select(models.OutboxObject)
            .where(
                models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
                models.OutboxObject.is_deleted.is_(False),
            )
            .order_by(models.OutboxObject.ap_published_at.desc())
            .limit(20)
        )
    ).all()
    return ActivityPubResponse(
        {
            "@context": ap.AS_EXTENDED_CTX,
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
async def featured(
    db_session: AsyncSession = Depends(get_db_session),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse:
    outbox_objects = (
        await db_session.scalars(
            select(models.OutboxObject)
            .filter(
                models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
                models.OutboxObject.is_deleted.is_(False),
                models.OutboxObject.is_pinned.is_(True),
            )
            .order_by(models.OutboxObject.ap_published_at.desc())
            .limit(5)
        )
    ).all()
    return ActivityPubResponse(
        {
            "@context": ap.AS_EXTENDED_CTX,
            "id": f"{ID}/featured",
            "type": "OrderedCollection",
            "totalItems": len(outbox_objects),
            "orderedItems": [ap.remove_context(a.ap_object) for a in outbox_objects],
        }
    )


async def _check_outbox_object_acl(
    request: Request,
    db_session: AsyncSession,
    ap_object: models.OutboxObject,
    httpsig_info: httpsig.HTTPSigInfo,
) -> None:
    if templates.is_current_user_admin(request):
        return None

    if ap_object.visibility in [
        ap.VisibilityEnum.PUBLIC,
        ap.VisibilityEnum.UNLISTED,
    ]:
        return None

    elif ap_object.visibility == ap.VisibilityEnum.FOLLOWERS_ONLY:
        # Is the signing actor a follower?
        followers = await boxes.fetch_actor_collection(
            db_session, BASE_URL + "/followers"
        )
        if httpsig_info.signed_by_ap_actor_id in [actor.ap_id for actor in followers]:
            return None

    elif ap_object.visibility == ap.VisibilityEnum.DIRECT:
        # Is the signing actor targeted in the object audience?
        audience = ap_object.ap_object.get("to", []) + ap_object.ap_object.get("cc", [])
        if httpsig_info.signed_by_ap_actor_id in audience:
            return None

    raise HTTPException(status_code=404)


@app.get("/o/{public_id}")
async def outbox_by_public_id(
    public_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    httpsig_info: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse | templates.TemplateResponse:
    maybe_object = (
        (
            await db_session.execute(
                select(models.OutboxObject)
                .options(
                    joinedload(models.OutboxObject.outbox_object_attachments).options(
                        joinedload(models.OutboxObjectAttachment.upload)
                    )
                )
                .where(
                    models.OutboxObject.public_id == public_id,
                    models.OutboxObject.is_deleted.is_(False),
                )
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if not maybe_object:
        raise HTTPException(status_code=404)

    await _check_outbox_object_acl(request, db_session, maybe_object, httpsig_info)

    if is_activitypub_requested(request):
        return ActivityPubResponse(maybe_object.ap_object)

    replies_tree = await boxes.get_replies_tree(db_session, maybe_object)

    likes = (
        (
            await db_session.scalars(
                select(models.InboxObject)
                .where(
                    models.InboxObject.ap_type == "Like",
                    models.InboxObject.activity_object_ap_id == maybe_object.ap_id,
                    models.InboxObject.is_deleted.is_(False),
                )
                .options(joinedload(models.InboxObject.actor))
                .order_by(models.InboxObject.ap_published_at.desc())
                .limit(10)
            )
        )
        .unique()
        .all()
    )

    shares = (
        (
            await db_session.scalars(
                select(models.InboxObject)
                .filter(
                    models.InboxObject.ap_type == "Announce",
                    models.InboxObject.activity_object_ap_id == maybe_object.ap_id,
                    models.InboxObject.is_deleted.is_(False),
                )
                .options(joinedload(models.InboxObject.actor))
                .order_by(models.InboxObject.ap_published_at.desc())
                .limit(10)
            )
        )
        .unique()
        .all()
    )

    webmentions = (
        await db_session.scalars(
            select(models.Webmention)
            .filter(
                models.Webmention.outbox_object_id == maybe_object.id,
                models.Webmention.is_deleted.is_(False),
            )
            .limit(10)
        )
    ).all()

    return await templates.render_template(
        db_session,
        request,
        "object.html",
        {
            "replies_tree": replies_tree,
            "outbox_object": maybe_object,
            "likes": likes,
            "shares": shares,
            "webmentions": webmentions,
        },
    )


@app.get("/o/{public_id}/activity")
async def outbox_activity_by_public_id(
    public_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    httpsig_info: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse:
    maybe_object = (
        await db_session.execute(
            select(models.OutboxObject).where(
                models.OutboxObject.public_id == public_id,
                models.OutboxObject.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not maybe_object:
        raise HTTPException(status_code=404)

    await _check_outbox_object_acl(request, db_session, maybe_object, httpsig_info)

    return ActivityPubResponse(ap.wrap_object(maybe_object.ap_object))


@app.get("/t/{tag}")
async def tag_by_name(
    tag: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    _: httpsig.HTTPSigInfo = Depends(httpsig.httpsig_checker),
) -> ActivityPubResponse | templates.TemplateResponse:
    where = [
        models.TaggedOutboxObject.tag == tag,
        models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        models.OutboxObject.is_deleted.is_(False),
    ]
    tagged_count = await db_session.scalar(
        select(func.count(models.OutboxObject.id))
        .join(models.TaggedOutboxObject)
        .where(*where)
    )
    if not tagged_count:
        raise HTTPException(status_code=404)

    if is_activitypub_requested(request):
        outbox_object_ids = await db_session.execute(
            select(models.OutboxObject.ap_id)
            .join(
                models.TaggedOutboxObject,
                models.TaggedOutboxObject.outbox_object_id == models.OutboxObject.id,
            )
            .where(*where)
            .order_by(models.OutboxObject.ap_published_at.desc())
            .limit(20)
        )
        return ActivityPubResponse(
            {
                "@context": ap.AS_CTX,
                "id": BASE_URL + f"/t/{tag}",
                "type": "OrderedCollection",
                "totalItems": tagged_count,
                "orderedItems": [
                    outbox_object.ap_id for outbox_object in outbox_object_ids
                ],
            }
        )

    outbox_objects_result = await db_session.scalars(
        select(models.OutboxObject)
        .where(*where)
        .join(
            models.TaggedOutboxObject,
            models.TaggedOutboxObject.outbox_object_id == models.OutboxObject.id,
        )
        .options(
            joinedload(models.OutboxObject.outbox_object_attachments).options(
                joinedload(models.OutboxObjectAttachment.upload)
            )
        )
        .order_by(models.OutboxObject.ap_published_at.desc())
        .limit(20)
    )
    outbox_objects = outbox_objects_result.unique().all()

    return await templates.render_template(
        db_session,
        request,
        "index.html",
        {
            "request": request,
            "objects": outbox_objects,
        },
    )


@app.get("/e/{name}")
def emoji_by_name(name: str) -> ActivityPubResponse:
    try:
        emoji = EMOJIS_BY_NAME[f":{name}:"]
    except KeyError:
        raise HTTPException(status_code=404)

    return ActivityPubResponse({"@context": ap.AS_EXTENDED_CTX, **emoji})


@app.post("/inbox")
async def inbox(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    httpsig_info: httpsig.HTTPSigInfo = Depends(httpsig.enforce_httpsig),
) -> Response:
    logger.info(f"headers={request.headers}")
    payload = await request.json()
    logger.info(f"{payload=}")
    await new_ap_incoming_activity(db_session, httpsig_info, payload)
    return Response(status_code=202)


@app.get("/remote_follow")
async def get_remote_follow(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    return await templates.render_template(
        db_session,
        request,
        "remote_follow.html",
        {},
    )


@app.post("/remote_follow")
async def post_remote_follow(
    request: Request,
    csrf_check: None = Depends(verify_csrf_token),
    profile: str = Form(),
) -> RedirectResponse:
    if not profile.startswith("@"):
        profile = f"@{profile}"

    remote_follow_template = await get_remote_follow_template(profile)
    if not remote_follow_template:
        # TODO(ts): error message to user
        raise HTTPException(status_code=404)

    return RedirectResponse(
        remote_follow_template.format(uri=ID),
        status_code=302,
    )


@app.get("/.well-known/webfinger")
async def wellknown_webfinger(resource: str) -> JSONResponse:
    """Exposes/servers WebFinger data."""
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
async def nodeinfo(
    db_session: AsyncSession = Depends(get_db_session),
):
    local_posts = await public_outbox_objects_count(db_session)
    return JSONResponse(
        {
            "version": "2.1",
            "software": {
                "name": "microblogpub",
                "version": config.VERSION,
                "repository": "https://sr.ht/~tsileo/microblog.pub",
                "homepage": "https://docs.microblog.pub",
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


proxy_client = httpx.AsyncClient(follow_redirects=True, http2=True)


async def _proxy_get(
    request: starlette.requests.Request, url: str, stream: bool
) -> httpx.Response:
    # Request the URL (and filter request headers)
    proxy_req = proxy_client.build_request(
        request.method,
        url,
        headers=[
            (k, v)
            for (k, v) in request.headers.raw
            if k.lower()
            not in [
                b"host",
                b"cookie",
                b"x-forwarded-for",
                b"x-forwarded-proto",
                b"x-real-ip",
                b"user-agent",
            ]
        ]
        + [(b"user-agent", USER_AGENT.encode())],
    )
    return await proxy_client.send(proxy_req, stream=stream)


def _filter_proxy_resp_headers(
    proxy_resp: httpx.Response,
    allowed_headers: list[str],
) -> dict[str, str]:
    return {
        k: v for (k, v) in proxy_resp.headers.items() if k.lower() in allowed_headers
    }


@app.get("/proxy/media/{encoded_url}")
async def serve_proxy_media(request: Request, encoded_url: str) -> StreamingResponse:
    # Decode the base64-encoded URL
    url = base64.urlsafe_b64decode(encoded_url).decode()
    check_url(url)

    proxy_resp = await _proxy_get(request, url, stream=True)

    return StreamingResponse(
        proxy_resp.aiter_raw(),
        status_code=proxy_resp.status_code,
        headers=_filter_proxy_resp_headers(
            proxy_resp,
            [
                "content-length",
                "content-type",
                "content-range",
                "accept-ranges" "etag",
                "cache-control",
                "expires",
                "date",
                "last-modified",
            ],
        ),
        background=BackgroundTask(proxy_resp.aclose),
    )


@app.get("/proxy/media/{encoded_url}/{size}")
async def serve_proxy_media_resized(
    request: Request,
    encoded_url: str,
    size: int,
) -> PlainTextResponse:
    if size not in {50, 740}:
        raise ValueError("Unsupported size")

    # Decode the base64-encoded URL
    url = base64.urlsafe_b64decode(encoded_url).decode()
    check_url(url)

    if cached_resp := _RESIZED_CACHE.get((url, size)):
        resized_content, resized_mimetype, resp_headers = cached_resp
        return PlainTextResponse(
            resized_content,
            media_type=resized_mimetype,
            headers=resp_headers,
        )

    proxy_resp = await _proxy_get(request, url, stream=False)
    if proxy_resp.status_code != 200:
        return PlainTextResponse(
            proxy_resp.content,
            status_code=proxy_resp.status_code,
        )

    # Filter the headers
    proxy_resp_headers = _filter_proxy_resp_headers(
        proxy_resp,
        [
            "content-type",
            "etag",
            "cache-control",
            "expires",
            "last-modified",
        ],
    )

    try:
        out = BytesIO(proxy_resp.content)
        i = Image.open(out)
        if getattr(i, "is_animated", False):
            raise ValueError
        i.thumbnail((size, size))
        resized_buf = BytesIO()
        i.save(resized_buf, format=i.format)
        resized_buf.seek(0)
        resized_content = resized_buf.read()
        resized_mimetype = i.get_format_mimetype()  # type: ignore

        # Only cache images < 1MB
        if len(resized_content) < 2**20:
            _RESIZED_CACHE[(url, size)] = (
                resized_content,
                resized_mimetype,
                proxy_resp_headers,
            )
        return PlainTextResponse(
            resized_content,
            media_type=resized_mimetype,
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
async def serve_attachment(
    content_hash: str,
    filename: str,
    db_session: AsyncSession = Depends(get_db_session),
):
    upload = (
        await db_session.execute(
            select(models.Upload).where(
                models.Upload.content_hash == content_hash,
            )
        )
    ).scalar_one_or_none()
    if not upload:
        raise HTTPException(status_code=404)

    return FileResponse(
        UPLOAD_DIR / content_hash,
        media_type=upload.content_type,
    )


@app.get("/attachments/thumbnails/{content_hash}/{filename}")
async def serve_attachment_thumbnail(
    content_hash: str,
    filename: str,
    db_session: AsyncSession = Depends(get_db_session),
):
    upload = (
        await db_session.execute(
            select(models.Upload).where(
                models.Upload.content_hash == content_hash,
            )
        )
    ).scalar_one_or_none()
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
Disallow: /admin
Disallow: /remote_follow"""


async def _get_outbox_for_feed(db_session: AsyncSession) -> list[models.OutboxObject]:
    return (
        (
            await db_session.scalars(
                select(models.OutboxObject)
                .where(
                    models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
                    models.OutboxObject.is_deleted.is_(False),
                    models.OutboxObject.ap_type.in_(["Note", "Article", "Video"]),
                )
                .options(
                    joinedload(models.OutboxObject.outbox_object_attachments).options(
                        joinedload(models.OutboxObjectAttachment.upload)
                    )
                )
                .order_by(models.OutboxObject.ap_published_at.desc())
                .limit(20)
            )
        )
        .unique()
        .all()
    )


@app.get("/feed.json")
async def json_feed(
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    outbox_objects = await _get_outbox_for_feed(db_session)
    data = []
    for outbox_object in outbox_objects:
        if not outbox_object.ap_published_at:
            raise ValueError(f"{outbox_object} has no published date")
        data.append(
            {
                "id": outbox_object.public_id,
                "url": outbox_object.url,
                "content_html": outbox_object.content,
                "content_text": outbox_object.source,
                "date_published": outbox_object.ap_published_at.isoformat(),
                "attachments": [
                    {"url": a.url, "mime_type": a.media_type}
                    for a in outbox_object.attachments
                ],
            }
        )
    return {
        "version": "https://jsonfeed.org/version/1",
        "title": f"{LOCAL_ACTOR.display_name}'s microblog'",
        "home_page_url": LOCAL_ACTOR.url,
        "feed_url": BASE_URL + "/feed.json",
        "author": {
            "name": LOCAL_ACTOR.display_name,
            "url": LOCAL_ACTOR.url,
            "avatar": LOCAL_ACTOR.icon_url,
        },
        "items": data,
    }


async def _gen_rss_feed(
    db_session: AsyncSession,
):
    fg = FeedGenerator()
    fg.id(BASE_URL + "/feed.rss")
    fg.title(f"{LOCAL_ACTOR.display_name}'s microblog")
    fg.description(f"{LOCAL_ACTOR.display_name}'s microblog")
    fg.author({"name": LOCAL_ACTOR.display_name})
    fg.link(href=LOCAL_ACTOR.url, rel="alternate")
    fg.logo(LOCAL_ACTOR.icon_url)
    fg.language("en")

    outbox_objects = await _get_outbox_for_feed(db_session)
    for outbox_object in outbox_objects:
        if not outbox_object.ap_published_at:
            raise ValueError(f"{outbox_object} has no published date")

        content = outbox_object.content
        if content is None:
            raise ValueError("Should never happen")

        if outbox_object.attachments:
            for attachment in outbox_object.attachments:
                if attachment.type == "Image" or (
                    attachment.media_type and attachment.media_type.startswith("image")
                ):
                    content += f'<img src="{attachment.url}">'
                # TODO(ts): other attachment types

        fe = fg.add_entry()
        fe.id(outbox_object.url)
        fe.link(href=outbox_object.url)
        fe.title(outbox_object.url)
        fe.description(content)
        fe.content(content)
        fe.published(outbox_object.ap_published_at.replace(tzinfo=timezone.utc))

    return fg


@app.get("/feed.rss")
async def rss_feed(
    db_session: AsyncSession = Depends(get_db_session),
) -> PlainTextResponse:
    return PlainTextResponse(
        (await _gen_rss_feed(db_session)).rss_str(),
        headers={"Content-Type": "application/rss+xml"},
    )


@app.get("/feed.atom")
async def atom_feed(
    db_session: AsyncSession = Depends(get_db_session),
) -> PlainTextResponse:
    return PlainTextResponse(
        (await _gen_rss_feed(db_session)).atom_str(),
        headers={"Content-Type": "application/atom+xml"},
    )
