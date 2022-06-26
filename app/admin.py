from fastapi import APIRouter
from fastapi import Cookie
from fastapi import Depends
from fastapi import Form
from fastapi import Request
from fastapi import UploadFile
from fastapi.exceptions import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app import activitypub as ap
from app import boxes
from app import models
from app import templates
from app.actor import LOCAL_ACTOR
from app.actor import get_actors_metadata
from app.boxes import get_inbox_object_by_ap_id
from app.boxes import get_outbox_object_by_ap_id
from app.boxes import send_follow
from app.config import generate_csrf_token
from app.config import session_serializer
from app.config import verify_csrf_token
from app.config import verify_password
from app.database import get_db
from app.lookup import lookup
from app.uploads import save_upload


def user_session_or_redirect(
    request: Request,
    session: str | None = Cookie(default=None),
) -> None:
    _RedirectToLoginPage = HTTPException(
        status_code=302,
        headers={"Location": request.url_for("login")},
    )

    if not session:
        raise _RedirectToLoginPage

    try:
        loaded_session = session_serializer.loads(session, max_age=3600 * 12)
    except Exception:
        raise _RedirectToLoginPage

    if not loaded_session.get("is_logged_in"):
        raise _RedirectToLoginPage

    return None


router = APIRouter(
    dependencies=[Depends(user_session_or_redirect)],
)
unauthenticated_router = APIRouter()


@router.get("/")
def admin_index(
    request: Request,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    return templates.render_template(db, request, "index.html", {"request": request})


@router.get("/lookup")
def get_lookup(
    request: Request,
    query: str | None = None,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    ap_object = None
    actors_metadata = {}
    if query:
        ap_object = lookup(db, query)
        if ap_object.ap_type in ap.ACTOR_TYPES:
            actors_metadata = get_actors_metadata(db, [ap_object])  # type: ignore
        else:
            actors_metadata = get_actors_metadata(db, [ap_object.actor])  # type: ignore
        print(ap_object)
    return templates.render_template(
        db,
        request,
        "lookup.html",
        {
            "query": query,
            "ap_object": ap_object,
            "actors_metadata": actors_metadata,
        },
    )


@router.get("/new")
def admin_new(
    request: Request,
    query: str | None = None,
    in_reply_to: str | None = None,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    content = ""
    in_reply_to_object = None
    if in_reply_to:
        in_reply_to_object = boxes.get_anybox_object_by_ap_id(db, in_reply_to)

        # Add mentions to the initial note content
        if not in_reply_to_object:
            raise ValueError(f"Unknown object {in_reply_to=}")
        if in_reply_to_object.actor.ap_id != LOCAL_ACTOR.ap_id:
            content += f"{in_reply_to_object.actor.handle} "
        for tag in in_reply_to_object.tags:
            if tag.get("type") == "Mention" and tag["name"] != LOCAL_ACTOR.handle:
                content += f'{tag["name"]} '

    return templates.render_template(
        db,
        request,
        "admin_new.html",
        {
            "in_reply_to_object": in_reply_to_object,
            "content": content,
            "visibility_choices": [
                (v.name, ap.VisibilityEnum.get_display_name(v))
                for v in ap.VisibilityEnum
            ],
        },
    )


@router.get("/stream")
def stream(
    request: Request,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    stream = (
        db.query(models.InboxObject)
        .filter(
            models.InboxObject.ap_type.in_(["Note", "Article", "Video", "Announce"]),
            models.InboxObject.is_hidden_from_stream.is_(False),
            models.InboxObject.undone_by_inbox_object_id.is_(None),
        )
        .options(
            # joinedload(models.InboxObject.relates_to_inbox_object),
            joinedload(models.InboxObject.relates_to_outbox_object),
        )
        .order_by(models.InboxObject.ap_published_at.desc())
        .limit(20)
        .all()
    )
    return templates.render_template(
        db,
        request,
        "admin_stream.html",
        {
            "stream": stream,
        },
    )


@router.get("/bookmarks")
def admin_bookmarks(
    request: Request,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    stream = (
        db.query(models.InboxObject)
        .filter(
            models.InboxObject.ap_type.in_(["Note", "Article", "Video", "Announce"]),
            models.InboxObject.is_hidden_from_stream.is_(False),
            models.InboxObject.undone_by_inbox_object_id.is_(None),
            models.InboxObject.is_bookmarked.is_(True),
        )
        .order_by(models.InboxObject.ap_published_at.desc())
        .limit(20)
        .all()
    )
    return templates.render_template(
        db,
        request,
        "admin_stream.html",
        {
            "stream": stream,
        },
    )


@router.get("/inbox")
def admin_inbox(
    request: Request,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    inbox = (
        db.query(models.InboxObject)
        .options(
            joinedload(models.InboxObject.relates_to_inbox_object),
            joinedload(models.InboxObject.relates_to_outbox_object),
        )
        .order_by(models.InboxObject.ap_published_at.desc())
        .limit(20)
        .all()
    )
    return templates.render_template(
        db,
        request,
        "admin_inbox.html",
        {
            "inbox": inbox,
        },
    )


@router.get("/outbox")
def admin_outbox(
    request: Request, db: Session = Depends(get_db), filter_by: str | None = None
) -> templates.TemplateResponse:
    q = db.query(models.OutboxObject).filter(
        models.OutboxObject.ap_type.not_in(["Accept"])
    )
    if filter_by:
        q = q.filter(models.OutboxObject.ap_type == filter_by)

    outbox = (
        q.options(
            joinedload(models.OutboxObject.relates_to_inbox_object),
            joinedload(models.OutboxObject.relates_to_outbox_object),
            joinedload(models.OutboxObject.relates_to_actor),
        )
        .order_by(models.OutboxObject.ap_published_at.desc())
        .limit(20)
        .all()
    )
    actors_metadata = get_actors_metadata(
        db,
        [
            outbox_object.relates_to_actor
            for outbox_object in outbox
            if outbox_object.relates_to_actor
        ],
    )

    return templates.render_template(
        db,
        request,
        "admin_outbox.html",
        {
            "actors_metadata": actors_metadata,
            "outbox": outbox,
        },
    )


@router.get("/notifications")
def get_notifications(
    request: Request, db: Session = Depends(get_db)
) -> templates.TemplateResponse:
    notifications = (
        db.query(models.Notification)
        .options(
            joinedload(models.Notification.actor),
            joinedload(models.Notification.inbox_object),
            joinedload(models.Notification.outbox_object),
        )
        .order_by(models.Notification.created_at.desc())
        .all()
    )
    actors_metadata = get_actors_metadata(
        db, [notif.actor for notif in notifications if notif.actor]
    )

    for notif in notifications:
        notif.is_new = False
    db.commit()

    return templates.render_template(
        db,
        request,
        "notifications.html",
        {
            "notifications": notifications,
            "actors_metadata": actors_metadata,
        },
    )


@router.get("/object")
def admin_object(
    request: Request,
    ap_id: str,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    requested_object = boxes.get_anybox_object_by_ap_id(db, ap_id)
    if not requested_object:
        raise HTTPException(status_code=404)

    replies_tree = boxes.get_replies_tree(db, requested_object)

    return templates.render_template(
        db,
        request,
        "object.html",
        {"replies_tree": replies_tree},
    )


@router.get("/profile")
def admin_profile(
    request: Request,
    actor_id: str,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    actor = db.query(models.Actor).filter(models.Actor.ap_id == actor_id).one_or_none()
    if not actor:
        raise HTTPException(status_code=404)

    actors_metadata = get_actors_metadata(db, [actor])

    inbox_objects = (
        db.query(models.InboxObject)
        .filter(
            models.InboxObject.actor_id == actor.id,
            models.InboxObject.ap_type.in_(["Note", "Article", "Video"]),
        )
        .order_by(models.InboxObject.ap_published_at.desc())
        .all()
    )

    return templates.render_template(
        db,
        request,
        "admin_profile.html",
        {
            "actors_metadata": actors_metadata,
            "actor": actor,
            "inbox_objects": inbox_objects,
        },
    )


@router.post("/actions/follow")
def admin_actions_follow(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    print(f"Following {ap_actor_id}")
    send_follow(db, ap_actor_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/like")
def admin_actions_like(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    boxes.send_like(db, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/undo")
def admin_actions_undo(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    boxes.send_undo(db, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/announce")
def admin_actions_announce(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    boxes.send_announce(db, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/bookmark")
def admin_actions_bookmark(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    inbox_object = get_inbox_object_by_ap_id(db, ap_object_id)
    if not inbox_object:
        raise ValueError("Should never happen")
    inbox_object.is_bookmarked = True
    db.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unbookmark")
def admin_actions_unbookmark(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    inbox_object = get_inbox_object_by_ap_id(db, ap_object_id)
    if not inbox_object:
        raise ValueError("Should never happen")
    inbox_object.is_bookmarked = False
    db.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/pin")
def admin_actions_pin(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    outbox_object = get_outbox_object_by_ap_id(db, ap_object_id)
    if not outbox_object:
        raise ValueError("Should never happen")
    outbox_object.is_pinned = True
    db.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unpin")
def admin_actions_unpin(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    outbox_object = get_outbox_object_by_ap_id(db, ap_object_id)
    if not outbox_object:
        raise ValueError("Should never happen")
    outbox_object.is_pinned = False
    db.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/new")
def admin_actions_new(
    request: Request,
    files: list[UploadFile],
    content: str = Form(),
    redirect_url: str = Form(),
    in_reply_to: str | None = Form(None),
    visibility: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    # XXX: for some reason, no files restuls in an empty single file
    uploads = []
    if len(files) >= 1 and files[0].filename:
        for f in files:
            upload = save_upload(db, f)
            uploads.append((upload, f.filename))
    public_id = boxes.send_create(
        db,
        source=content,
        uploads=uploads,
        in_reply_to=in_reply_to or None,
        visibility=ap.VisibilityEnum[visibility],
    )
    return RedirectResponse(
        request.url_for("outbox_by_public_id", public_id=public_id),
        status_code=302,
    )


@unauthenticated_router.get("/login")
def login(
    request: Request,
    db: Session = Depends(get_db),
) -> templates.TemplateResponse:
    return templates.render_template(
        db,
        request,
        "login.html",
        {"csrf_token": generate_csrf_token()},
    )


@unauthenticated_router.post("/login")
def login_validation(
    request: Request,
    password: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
) -> RedirectResponse:
    if not verify_password(password):
        raise HTTPException(status_code=401)

    resp = RedirectResponse("/admin", status_code=302)
    resp.set_cookie("session", session_serializer.dumps({"is_logged_in": True}))  # type: ignore  # noqa: E501

    return resp


@router.get("/logout")
def logout(
    request: Request,
) -> RedirectResponse:
    resp = RedirectResponse(request.url_for("index"), status_code=302)
    resp.set_cookie("session", session_serializer.dumps({"is_logged_in": False}))  # type: ignore  # noqa: E501
    return resp
