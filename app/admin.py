from datetime import datetime

import httpx
from fastapi import APIRouter
from fastapi import Cookie
from fastapi import Depends
from fastapi import Form
from fastapi import Request
from fastapi import UploadFile
from fastapi.exceptions import HTTPException
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import and_
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app import activitypub as ap
from app import boxes
from app import models
from app import templates
from app.actor import LOCAL_ACTOR
from app.actor import fetch_actor
from app.actor import get_actors_metadata
from app.boxes import get_inbox_object_by_ap_id
from app.boxes import get_outbox_object_by_ap_id
from app.boxes import send_block
from app.boxes import send_follow
from app.boxes import send_unblock
from app.config import EMOJIS
from app.config import SESSION_TIMEOUT
from app.config import generate_csrf_token
from app.config import session_serializer
from app.config import verify_csrf_token
from app.config import verify_password
from app.database import AsyncSession
from app.database import get_db_session
from app.lookup import lookup
from app.templates import is_current_user_admin
from app.uploads import save_upload
from app.utils import pagination
from app.utils.emoji import EMOJIS_BY_NAME


async def user_session_or_redirect(
    request: Request,
    session: str | None = Cookie(default=None),
) -> None:
    if request.method == "POST":
        form_data = await request.form()
        if "redirect_url" in form_data:
            redirect_url = form_data["redirect_url"]
        else:
            redirect_url = request.url_for("admin_stream")
    else:
        redirect_url = str(request.url)

    _RedirectToLoginPage = HTTPException(
        status_code=302,
        headers={"Location": request.url_for("login") + f"?redirect={redirect_url}"},
    )

    if not session:
        logger.info("No existing admin session")
        raise _RedirectToLoginPage

    try:
        loaded_session = session_serializer.loads(session, max_age=SESSION_TIMEOUT)
    except Exception:
        logger.exception("Failed to validate admin session")
        raise _RedirectToLoginPage

    if not loaded_session.get("is_logged_in"):
        logger.info(f"Admin session invalidated: {loaded_session}")
        raise _RedirectToLoginPage

    return None


router = APIRouter(
    dependencies=[Depends(user_session_or_redirect)],
)
unauthenticated_router = APIRouter()


@router.get("/lookup")
async def get_lookup(
    request: Request,
    query: str | None = None,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse | RedirectResponse:
    error = None
    ap_object = None
    actors_metadata = {}
    if query:
        try:
            ap_object = await lookup(db_session, query)
        except httpx.TimeoutException:
            error = ap.FetchErrorTypeEnum.TIMEOUT
        except (ap.ObjectNotFoundError, ap.ObjectIsGoneError):
            error = ap.FetchErrorTypeEnum.NOT_FOUND
        except (ap.ObjectUnavailableError):
            error = ap.FetchErrorTypeEnum.UNAUHTORIZED
        except Exception:
            logger.exception(f"Failed to lookup {query}")
            error = ap.FetchErrorTypeEnum.INTERNAL_ERROR
        else:
            if ap_object.ap_type in ap.ACTOR_TYPES:
                try:
                    await fetch_actor(
                        db_session, ap_object.ap_id, save_if_not_found=False
                    )
                except ap.ObjectNotFoundError:
                    pass
                else:
                    return RedirectResponse(
                        request.url_for("admin_profile")
                        + f"?actor_id={ap_object.ap_id}",
                        status_code=302,
                    )

                actors_metadata = await get_actors_metadata(
                    db_session, [ap_object]  # type: ignore
                )
            else:
                # Check if the object is in the inbox
                requested_object = await boxes.get_anybox_object_by_ap_id(
                    db_session, ap_object.ap_id
                )
                if requested_object:
                    return RedirectResponse(
                        request.url_for("admin_object")
                        + f"?ap_id={ap_object.ap_id}#"
                        + requested_object.permalink_id,
                        status_code=302,
                    )

                actors_metadata = await get_actors_metadata(
                    db_session, [ap_object.actor]  # type: ignore
                )

    return await templates.render_template(
        db_session,
        request,
        "lookup.html",
        {
            "query": query,
            "ap_object": ap_object,
            "actors_metadata": actors_metadata,
            "error": error,
        },
    )


@router.get("/new")
async def admin_new(
    request: Request,
    query: str | None = None,
    in_reply_to: str | None = None,
    with_content: str | None = None,
    with_visibility: str | None = None,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    content = ""
    content_warning = None
    in_reply_to_object = None
    if in_reply_to:
        in_reply_to_object = await boxes.get_anybox_object_by_ap_id(
            db_session, in_reply_to
        )
        if not in_reply_to_object:
            logger.info(f"Saving unknwown object {in_reply_to}")
            raw_object = await ap.fetch(in_reply_to)
            await boxes.save_object_to_inbox(db_session, raw_object)
            await db_session.commit()
            in_reply_to_object = await boxes.get_anybox_object_by_ap_id(
                db_session, in_reply_to
            )

        # Add mentions to the initial note content
        if not in_reply_to_object:
            raise ValueError(f"Unknown object {in_reply_to=}")
        if in_reply_to_object.actor.ap_id != LOCAL_ACTOR.ap_id:
            content += f"{in_reply_to_object.actor.handle} "
        for tag in in_reply_to_object.tags:
            if tag.get("type") == "Mention" and tag["name"] != LOCAL_ACTOR.handle:
                mentioned_actor = await fetch_actor(db_session, tag["href"])
                content += f"{mentioned_actor.handle} "

        # Copy the content warning if any
        if in_reply_to_object.summary:
            content_warning = in_reply_to_object.summary
    elif with_content:
        content += f"{with_content} "

    return await templates.render_template(
        db_session,
        request,
        "admin_new.html",
        {
            "in_reply_to_object": in_reply_to_object,
            "content": content,
            "content_warning": content_warning,
            "visibility_choices": [
                (v.name, ap.VisibilityEnum.get_display_name(v))
                for v in ap.VisibilityEnum
            ],
            "visibility": with_visibility,
            "emojis": EMOJIS.split(" "),
            "custom_emojis": sorted(
                [dat for name, dat in EMOJIS_BY_NAME.items()],
                key=lambda obj: obj["name"],
            ),
        },
    )


@router.get("/bookmarks")
async def admin_bookmarks(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    # TODO: support pagination
    stream = (
        (
            await db_session.scalars(
                select(models.InboxObject)
                .where(
                    models.InboxObject.ap_type.in_(
                        ["Note", "Article", "Video", "Announce"]
                    ),
                    models.InboxObject.is_bookmarked.is_(True),
                    models.InboxObject.is_deleted.is_(False),
                )
                .options(
                    joinedload(models.InboxObject.relates_to_inbox_object),
                    joinedload(models.InboxObject.relates_to_outbox_object).options(
                        joinedload(
                            models.OutboxObject.outbox_object_attachments
                        ).options(joinedload(models.OutboxObjectAttachment.upload)),
                    ),
                    joinedload(models.InboxObject.actor),
                )
                .order_by(models.InboxObject.ap_published_at.desc())
                .limit(20)
            )
        )
        .unique()
        .all()
    )
    return await templates.render_template(
        db_session,
        request,
        "admin_stream.html",
        {
            "stream": stream,
        },
    )


@router.get("/stream")
async def admin_stream(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        models.InboxObject.is_hidden_from_stream.is_(False),
        models.InboxObject.is_deleted.is_(False),
    ]
    if cursor:
        where.append(
            models.InboxObject.ap_published_at < pagination.decode_cursor(cursor)
        )

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(models.InboxObject.id)).where(*where)
    )
    q = select(models.InboxObject).where(*where)

    inbox = (
        (
            await db_session.scalars(
                q.options(
                    joinedload(models.InboxObject.relates_to_inbox_object).options(
                        joinedload(models.InboxObject.actor)
                    ),
                    joinedload(models.InboxObject.relates_to_outbox_object).options(
                        joinedload(
                            models.OutboxObject.outbox_object_attachments
                        ).options(joinedload(models.OutboxObjectAttachment.upload)),
                    ),
                    joinedload(models.InboxObject.actor),
                )
                .order_by(models.InboxObject.ap_published_at.desc())
                .limit(20)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(inbox[-1].ap_published_at)
        if inbox and remaining_count > page_size
        else None
    )

    actors_metadata = await get_actors_metadata(
        db_session,
        [
            inbox_object.actor
            for inbox_object in inbox
            if inbox_object.ap_type == "Follow"
        ],
    )

    return await templates.render_template(
        db_session,
        request,
        "admin_inbox.html",
        {
            "inbox": inbox,
            "actors_metadata": actors_metadata,
            "next_cursor": next_cursor,
            "show_filters": False,
        },
    )


@router.get("/inbox")
async def admin_inbox(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    filter_by: str | None = None,
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        models.InboxObject.ap_type.not_in(
            [
                "Accept",
                "Delete",
                "Create",
                "Update",
                "Undo",
                "Read",
                "Reject",
                "Add",
                "Remove",
                "EmojiReact",
            ]
        ),
        models.InboxObject.is_deleted.is_(False),
        models.InboxObject.is_transient.is_(False),
    ]
    if filter_by:
        where.append(models.InboxObject.ap_type == filter_by)
    if cursor:
        where.append(
            models.InboxObject.ap_published_at < pagination.decode_cursor(cursor)
        )

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(models.InboxObject.id)).where(*where)
    )
    q = select(models.InboxObject).where(*where)

    inbox = (
        (
            await db_session.scalars(
                q.options(
                    joinedload(models.InboxObject.relates_to_inbox_object).options(
                        joinedload(models.InboxObject.actor)
                    ),
                    joinedload(models.InboxObject.relates_to_outbox_object).options(
                        joinedload(
                            models.OutboxObject.outbox_object_attachments
                        ).options(joinedload(models.OutboxObjectAttachment.upload)),
                    ),
                    joinedload(models.InboxObject.actor),
                )
                .order_by(models.InboxObject.ap_published_at.desc())
                .limit(20)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(inbox[-1].ap_published_at)
        if inbox and remaining_count > page_size
        else None
    )

    actors_metadata = await get_actors_metadata(
        db_session,
        [
            inbox_object.actor
            for inbox_object in inbox
            if inbox_object.ap_type == "Follow"
        ],
    )

    return await templates.render_template(
        db_session,
        request,
        "admin_inbox.html",
        {
            "inbox": inbox,
            "actors_metadata": actors_metadata,
            "next_cursor": next_cursor,
            "show_filters": True,
        },
    )


@router.get("/direct_messages")
async def admin_direct_messages(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
) -> templates.TemplateResponse:
    # The process for building DMs thread is a bit compex in term of query
    # but it does not require an extra tables to index/manage threads

    inbox_convos = (
        (
            await db_session.execute(
                select(
                    models.InboxObject.ap_context,
                    models.InboxObject.actor_id,
                    func.count(1).label("count"),
                    func.max(models.InboxObject.ap_published_at).label(
                        "most_recent_date"
                    ),
                )
                .where(
                    models.InboxObject.visibility == ap.VisibilityEnum.DIRECT,
                    models.InboxObject.ap_context.is_not(None),
                    # Skip transient object like poll relies
                    models.InboxObject.is_transient.is_(False),
                    models.InboxObject.is_deleted.is_(False),
                )
                .group_by(models.InboxObject.ap_context, models.InboxObject.actor_id)
            )
        )
        .unique()
        .all()
    )
    outbox_convos = (
        (
            await db_session.execute(
                select(
                    models.OutboxObject.ap_context,
                    func.count(1).label("count"),
                    func.max(models.OutboxObject.ap_published_at).label(
                        "most_recent_date"
                    ),
                )
                .where(
                    models.OutboxObject.visibility == ap.VisibilityEnum.DIRECT,
                    models.OutboxObject.ap_context.is_not(None),
                    # Skip transient object like poll relies
                    models.OutboxObject.is_transient.is_(False),
                    models.OutboxObject.is_deleted.is_(False),
                )
                .group_by(models.OutboxObject.ap_context)
            )
        )
        .unique()
        .all()
    )

    # Build a "threads index" by combining objects from the inbox and outbox
    convos = {}
    for inbox_convo in inbox_convos:
        if inbox_convo.ap_context not in convos:
            convos[inbox_convo.ap_context] = {
                "actor_ids": {inbox_convo.actor_id},
                "count": inbox_convo.count,
                "most_recent_from_inbox": inbox_convo.most_recent_date,
                "most_recent_from_outbox": datetime.min,
            }
        else:
            convos[inbox_convo.ap_context]["actor_ids"].add(inbox_convo.actor_id)
            convos[inbox_convo.ap_context]["count"] += inbox_convo.count
            convos[inbox_convo.ap_context]["most_recent_from_inbox"] = max(
                inbox_convo.most_recent_date,
                convos[inbox_convo.ap_context]["most_recent_from_inbox"],
            )

    for outbox_convo in outbox_convos:
        if outbox_convo.ap_context not in convos:
            convos[outbox_convo.ap_context] = {
                "actor_ids": set(),
                "count": outbox_convo.count,
                "most_recent_from_inbox": datetime.min,
                "most_recent_from_outbox": outbox_convo.most_recent_date,
            }
        else:
            convos[outbox_convo.ap_context]["count"] += outbox_convo.count
            convos[outbox_convo.ap_context]["most_recent_from_outbox"] = max(
                outbox_convo.most_recent_date,
                convos[outbox_convo.ap_context]["most_recent_from_outbox"],
            )

    # Fetch the latest object for each threads
    convos_with_last_from_inbox = []
    convos_with_last_from_outbox = []
    for context, convo in convos.items():
        if convo["most_recent_from_inbox"] > convo["most_recent_from_outbox"]:
            convos_with_last_from_inbox.append(
                and_(
                    models.InboxObject.ap_context == context,
                    models.InboxObject.ap_published_at
                    == convo["most_recent_from_inbox"],
                )
            )
        else:
            convos_with_last_from_outbox.append(
                and_(
                    models.OutboxObject.ap_context == context,
                    models.OutboxObject.ap_published_at
                    == convo["most_recent_from_outbox"],
                )
            )
    last_from_inbox = (
        (
            (
                await db_session.scalars(
                    select(models.InboxObject)
                    .where(or_(*convos_with_last_from_inbox))
                    .options(
                        joinedload(models.InboxObject.actor),
                    )
                )
            )
            .unique()
            .all()
        )
        if convos_with_last_from_inbox
        else []
    )
    last_from_outbox = (
        (
            (
                await db_session.scalars(
                    select(models.OutboxObject)
                    .where(or_(*convos_with_last_from_outbox))
                    .options(
                        joinedload(
                            models.OutboxObject.outbox_object_attachments
                        ).options(joinedload(models.OutboxObjectAttachment.upload)),
                    )
                )
            )
            .unique()
            .all()
        )
        if convos_with_last_from_outbox
        else []
    )

    # Build the template response
    threads = []
    for anybox_object in sorted(
        last_from_inbox + last_from_outbox,
        key=lambda x: x.ap_published_at,
        reverse=True,
    ):
        convo = convos[anybox_object.ap_context]
        actors = list(
            (
                await db_session.execute(
                    select(models.Actor).where(models.Actor.id.in_(convo["actor_ids"]))
                )
            ).scalars()
        )
        # If this message from outbox starts a thread with no replies, look
        # at the mentions
        if not actors and anybox_object.is_from_outbox:
            actors = (  # type: ignore
                await db_session.execute(
                    select(models.Actor).where(
                        models.Actor.ap_id.in_(
                            mention["href"]
                            for mention in anybox_object.tags
                            if mention["type"] == "Mention"
                        )
                    )
                )
            ).scalars()
        threads.append((anybox_object, convo, actors))

    return await templates.render_template(
        db_session,
        request,
        "admin_direct_messages.html",
        {
            "threads": threads,
        },
    )


@router.get("/outbox")
async def admin_outbox(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    filter_by: str | None = None,
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        models.OutboxObject.ap_type.not_in(["Accept", "Delete", "Update"]),
        models.OutboxObject.is_deleted.is_(False),
        models.OutboxObject.is_transient.is_(False),
    ]
    if filter_by:
        where.append(models.OutboxObject.ap_type == filter_by)
    if cursor:
        where.append(
            models.OutboxObject.ap_published_at < pagination.decode_cursor(cursor)
        )

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(models.OutboxObject.id)).where(*where)
    )
    q = select(models.OutboxObject).where(*where)

    outbox = (
        (
            await db_session.scalars(
                q.options(
                    joinedload(models.OutboxObject.relates_to_inbox_object).options(
                        joinedload(models.InboxObject.actor),
                    ),
                    joinedload(models.OutboxObject.relates_to_outbox_object),
                    joinedload(models.OutboxObject.relates_to_actor),
                    joinedload(models.OutboxObject.outbox_object_attachments).options(
                        joinedload(models.OutboxObjectAttachment.upload)
                    ),
                )
                .order_by(models.OutboxObject.ap_published_at.desc())
                .limit(page_size)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(outbox[-1].ap_published_at)
        if outbox and remaining_count > page_size
        else None
    )

    actors_metadata = await get_actors_metadata(
        db_session,
        [
            outbox_object.relates_to_actor
            for outbox_object in outbox
            if outbox_object.relates_to_actor
        ],
    )

    return await templates.render_template(
        db_session,
        request,
        "admin_outbox.html",
        {
            "actors_metadata": actors_metadata,
            "outbox": outbox,
            "next_cursor": next_cursor,
        },
    )


@router.get("/notifications")
async def get_notifications(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = []
    if cursor:
        decoded_cursor = pagination.decode_cursor(cursor)
        where.append(models.Notification.created_at < decoded_cursor)

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(models.Notification.id)).where(*where)
    )

    notifications = (
        (
            await db_session.scalars(
                select(models.Notification)
                .where(*where)
                .options(
                    joinedload(models.Notification.actor),
                    joinedload(models.Notification.inbox_object).options(
                        joinedload(models.InboxObject.actor)
                    ),
                    joinedload(models.Notification.outbox_object).options(
                        joinedload(
                            models.OutboxObject.outbox_object_attachments
                        ).options(joinedload(models.OutboxObjectAttachment.upload)),
                    ),
                    joinedload(models.Notification.webmention),
                )
                .order_by(models.Notification.created_at.desc())
                .limit(page_size)
            )
        )
        .unique()
        .all()
    )
    actors_metadata = await get_actors_metadata(
        db_session, [notif.actor for notif in notifications if notif.actor]
    )
    more_unread_count = 0
    next_cursor = None

    if notifications and remaining_count > page_size:
        decoded_next_cursor = notifications[-1].created_at
        next_cursor = pagination.encode_cursor(decoded_next_cursor)

        # If on the "see more" page there's more unread notification, we want
        # to display it next to the link
        more_unread_count = await db_session.scalar(
            select(func.count(models.Notification.id)).where(
                models.Notification.is_new.is_(True),
                models.Notification.created_at < decoded_next_cursor,
            )
        )

    # Render the template before we change the new flag on notifications
    tpl_resp = await templates.render_template(
        db_session,
        request,
        "notifications.html",
        {
            "notifications": notifications,
            "actors_metadata": actors_metadata,
            "next_cursor": next_cursor,
            "more_unread_count": more_unread_count,
        },
    )

    if len({notif.id for notif in notifications if notif.is_new}):
        for notif in notifications:
            notif.is_new = False
        await db_session.commit()

    return tpl_resp


@router.get("/object")
async def admin_object(
    request: Request,
    ap_id: str,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    requested_object = await boxes.get_anybox_object_by_ap_id(db_session, ap_id)
    if not requested_object or requested_object.is_deleted:
        raise HTTPException(status_code=404)

    replies_tree = await boxes.get_replies_tree(
        db_session,
        requested_object,
        is_current_user_admin=True,
    )

    return await templates.render_template(
        db_session,
        request,
        "object.html",
        {"replies_tree": replies_tree},
    )


@router.get("/profile")
async def admin_profile(
    request: Request,
    actor_id: str,
    cursor: str | None = None,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    # TODO: show featured/pinned
    actor = (
        await db_session.execute(
            select(models.Actor).where(models.Actor.ap_id == actor_id)
        )
    ).scalar_one_or_none()
    if not actor:
        raise HTTPException(status_code=404)

    actors_metadata = await get_actors_metadata(db_session, [actor])

    where = [
        models.InboxObject.is_deleted.is_(False),
        models.InboxObject.actor_id == actor.id,
        models.InboxObject.ap_type.in_(
            ["Note", "Article", "Video", "Page", "Announce"]
        ),
    ]
    if cursor:
        decoded_cursor = pagination.decode_cursor(cursor)
        where.append(models.InboxObject.ap_published_at < decoded_cursor)

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(models.InboxObject.id)).where(*where)
    )

    inbox_objects = (
        (
            await db_session.scalars(
                select(models.InboxObject)
                .where(*where)
                .options(
                    joinedload(models.InboxObject.relates_to_inbox_object).options(
                        joinedload(models.InboxObject.actor)
                    ),
                    joinedload(models.InboxObject.relates_to_outbox_object).options(
                        joinedload(
                            models.OutboxObject.outbox_object_attachments
                        ).options(joinedload(models.OutboxObjectAttachment.upload)),
                    ),
                    joinedload(models.InboxObject.actor),
                )
                .order_by(models.InboxObject.ap_published_at.desc())
                .limit(page_size)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(inbox_objects[-1].created_at)
        if inbox_objects and remaining_count > page_size
        else None
    )

    return await templates.render_template(
        db_session,
        request,
        "admin_profile.html",
        {
            "actors_metadata": actors_metadata,
            "actor": actor,
            "inbox_objects": inbox_objects,
            "next_cursor": next_cursor,
        },
    )


@router.post("/actions/force_delete")
async def admin_actions_force_delete(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    ap_object_to_delete = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not ap_object_to_delete:
        raise ValueError(f"Cannot find {ap_object_id}")

    logger.info(f"Deleting {ap_object_to_delete.ap_type}/{ap_object_to_delete.ap_id}")
    await boxes._revert_side_effect_for_deleted_object(
        db_session,
        None,
        ap_object_to_delete,
        None,
    )
    ap_object_to_delete.is_deleted = True
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/force_delete_webmention")
async def admin_actions_force_delete_webmention(
    request: Request,
    webmention_id: int = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    webmention = await boxes.get_webmention_by_id(db_session, webmention_id)
    if not webmention:
        raise ValueError(f"Cannot find {webmention_id}")
    if not webmention.outbox_object:
        raise ValueError(f"Missing related outbox object for {webmention_id}")

    # TODO: move this
    logger.info(f"Deleting {webmention_id}")
    webmention.is_deleted = True
    await db_session.flush()
    from app.webmentions import _handle_webmention_side_effects

    await _handle_webmention_side_effects(
        db_session, webmention, webmention.outbox_object
    )
    # Delete related notifications
    notif_deletion_result = await db_session.execute(
        delete(models.Notification)
        .where(models.Notification.webmention_id == webmention.id)
        .execution_options(synchronize_session=False)
    )
    logger.info(
        f"Deleted {notif_deletion_result.rowcount} notifications"  # type: ignore
    )
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/follow")
async def admin_actions_follow(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    logger.info(f"Following {ap_actor_id}")
    await send_follow(db_session, ap_actor_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/block")
async def admin_actions_block(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await send_block(db_session, ap_actor_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unblock")
async def admin_actions_unblock(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    logger.info(f"Unblocking {ap_actor_id}")
    await send_unblock(db_session, ap_actor_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/delete")
async def admin_actions_delete(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_delete(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/accept_incoming_follow")
async def admin_actions_accept_incoming_follow(
    request: Request,
    notification_id: int = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_accept(db_session, notification_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/reject_incoming_follow")
async def admin_actions_reject_incoming_follow(
    request: Request,
    notification_id: int = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_reject(db_session, notification_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/like")
async def admin_actions_like(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_like(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/undo")
async def admin_actions_undo(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_undo(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/announce")
async def admin_actions_announce(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_announce(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/bookmark")
async def admin_actions_bookmark(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        logger.info(f"Saving unknwown object {ap_object_id}")
        raw_object = await ap.fetch(ap_object_id)
        inbox_object = await boxes.save_object_to_inbox(db_session, raw_object)
    inbox_object.is_bookmarked = True
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unbookmark")
async def admin_actions_unbookmark(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        raise ValueError("Should never happen")
    inbox_object.is_bookmarked = False
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/pin")
async def admin_actions_pin(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    outbox_object = await get_outbox_object_by_ap_id(db_session, ap_object_id)
    if not outbox_object:
        raise ValueError("Should never happen")
    outbox_object.is_pinned = True
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unpin")
async def admin_actions_unpin(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    outbox_object = await get_outbox_object_by_ap_id(db_session, ap_object_id)
    if not outbox_object:
        raise ValueError("Should never happen")
    outbox_object.is_pinned = False
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/new")
async def admin_actions_new(
    request: Request,
    files: list[UploadFile] = [],
    content: str | None = Form(None),
    redirect_url: str = Form(),
    in_reply_to: str | None = Form(None),
    content_warning: str | None = Form(None),
    is_sensitive: bool = Form(False),
    visibility: str = Form(),
    poll_type: str | None = Form(None),
    name: str | None = Form(None),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    if not content and not content_warning:
        raise HTTPException(status_code=422, detail="Error: object must have a content")

    # Do like Mastodon, if there's only a CW with no content and some attachments,
    # swap the CW and the content
    if not content and content_warning and len(files) >= 1:
        content = content_warning
        is_sensitive = True
        content_warning = None

    if not content:
        raise HTTPException(status_code=422, detail="Error: objec must have a content")

    # XXX: for some reason, no files restuls in an empty single file
    uploads = []
    raw_form_data = await request.form()
    if len(files) >= 1 and files[0].filename:
        for f in files:
            upload = await save_upload(db_session, f)
            uploads.append((upload, f.filename, raw_form_data.get("alt_" + f.filename)))

    ap_type = "Note"

    poll_duration_in_minutes = None
    poll_answers = None
    if poll_type:
        ap_type = "Question"
        poll_answers = []
        for i in ["1", "2", "3", "4"]:
            if answer := raw_form_data.get(f"poll_answer_{i}"):
                poll_answers.append(answer)

        if not poll_answers or len(poll_answers) < 2:
            raise ValueError("Question must have at least 2 answers")

        poll_duration_in_minutes = int(raw_form_data["poll_duration"])
    elif name:
        ap_type = "Article"

    public_id = await boxes.send_create(
        db_session,
        ap_type=ap_type,
        source=content,
        uploads=uploads,
        in_reply_to=in_reply_to or None,
        visibility=ap.VisibilityEnum[visibility],
        content_warning=content_warning or None,
        is_sensitive=True if content_warning else is_sensitive,
        poll_type=poll_type,
        poll_answers=poll_answers,
        poll_duration_in_minutes=poll_duration_in_minutes,
        name=name,
    )
    return RedirectResponse(
        request.url_for("outbox_by_public_id", public_id=public_id),
        status_code=302,
    )


@router.post("/actions/vote")
async def admin_actions_vote(
    request: Request,
    redirect_url: str = Form(),
    in_reply_to: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    form_data = await request.form()
    names = form_data.getlist("name")
    logger.info(f"{names=}")
    await boxes.send_vote(
        db_session,
        in_reply_to=in_reply_to,
        names=names,
    )
    return RedirectResponse(redirect_url, status_code=302)


@unauthenticated_router.get("/login")
async def login(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse | RedirectResponse:
    if is_current_user_admin(request):
        return RedirectResponse(request.url_for("admin_stream"), status_code=302)

    return await templates.render_template(
        db_session,
        request,
        "login.html",
        {
            "csrf_token": generate_csrf_token(),
            "redirect": request.query_params.get("redirect", ""),
        },
    )


@unauthenticated_router.post("/login")
async def login_validation(
    request: Request,
    password: str = Form(),
    redirect: str | None = Form(None),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse | templates.TemplateResponse:
    if not verify_password(password):
        logger.warning("Invalid password")
        return await templates.render_template(
            db_session,
            request,
            "login.html",
            {
                "error": "Invalid password",
                "csrf_token": generate_csrf_token(),
                "redirect": request.query_params.get("redirect", ""),
            },
            status_code=403,
        )

    resp = RedirectResponse(
        redirect or request.url_for("admin_stream"), status_code=302
    )
    resp.set_cookie("session", session_serializer.dumps({"is_logged_in": True}))  # type: ignore  # noqa: E501

    return resp


@router.get("/logout")
async def logout(
    request: Request,
) -> RedirectResponse:
    resp = RedirectResponse(request.url_for("index"), status_code=302)
    resp.set_cookie("session", session_serializer.dumps({"is_logged_in": False}))  # type: ignore  # noqa: E501
    return resp
