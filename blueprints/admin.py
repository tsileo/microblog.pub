import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import List
from urllib.parse import urlparse

import flask
from flask import abort
from flask import current_app as app
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from little_boxes import activitypub as ap
from little_boxes.webfinger import get_actor_url
from passlib.hash import bcrypt
from u2flib_server import u2f

import config
from config import DB
from config import ID
from config import PASS
from core.activitypub import Box
from core.activitypub import post_to_outbox
from core.db import find_one_activity
from core.meta import by_object_id
from core.meta import by_type
from core.shared import MY_PERSON
from core.shared import _build_thread
from core.shared import _Response
from core.shared import csrf
from core.shared import login_required
from core.shared import noindex
from core.shared import p
from core.shared import paginated_query
from utils import now
from utils.lookup import lookup

blueprint = flask.Blueprint("admin", __name__)


def verify_pass(pwd):
    return bcrypt.verify(pwd, PASS)


@blueprint.route("/admin/update_actor")
@login_required
def admin_update_actor() -> _Response:
    update = ap.Update(
        actor=MY_PERSON.id,
        object=MY_PERSON.to_dict(),
        to=[MY_PERSON.followers],
        cc=[ap.AS_PUBLIC],
        published=now(),
    )

    post_to_outbox(update)
    return "OK"


@blueprint.route("/admin/logout")
@login_required
def admin_logout() -> _Response:
    session["logged_in"] = False
    return redirect("/")


@blueprint.route("/login", methods=["POST", "GET"])
@noindex
def admin_login() -> _Response:
    if session.get("logged_in") is True:
        return redirect(url_for("admin.admin_notifications"))

    devices = [doc["device"] for doc in DB.u2f.find()]
    u2f_enabled = True if devices else False
    if request.method == "POST":
        csrf.protect()
        # 1. Check regular password login flow
        pwd = request.form.get("pass")
        if pwd:
            if verify_pass(pwd):
                session["logged_in"] = True
                return redirect(
                    request.args.get("redirect") or url_for("admin.admin_notifications")
                )
            else:
                abort(403)
        # 2. Check for U2F payload, if any
        elif devices:
            resp = json.loads(request.form.get("resp"))  # type: ignore
            try:
                u2f.complete_authentication(session["challenge"], resp)
            except ValueError as exc:
                print("failed", exc)
                abort(403)
                return
            finally:
                session["challenge"] = None

            session["logged_in"] = True
            return redirect(
                request.args.get("redirect") or url_for("admin.admin_notifications")
            )
        else:
            abort(401)

    payload = None
    if devices:
        payload = u2f.begin_authentication(ID, devices)
        session["challenge"] = payload

    return render_template("login.html", u2f_enabled=u2f_enabled, payload=payload)


@blueprint.route("/admin", methods=["GET"])
@login_required
def admin_index() -> _Response:
    q = {
        "meta.deleted": False,
        "meta.undo": False,
        "type": ap.ActivityType.LIKE.value,
        "box": Box.OUTBOX.value,
    }
    col_liked = DB.activities.count(q)

    return render_template(
        "admin.html",
        instances=list(DB.instances.find()),
        inbox_size=DB.activities.count({"box": Box.INBOX.value}),
        outbox_size=DB.activities.count({"box": Box.OUTBOX.value}),
        col_liked=col_liked,
        col_followers=DB.activities.count(
            {
                "box": Box.INBOX.value,
                "type": ap.ActivityType.FOLLOW.value,
                "meta.undo": False,
            }
        ),
        col_following=DB.activities.count(
            {
                "box": Box.OUTBOX.value,
                "type": ap.ActivityType.FOLLOW.value,
                "meta.undo": False,
            }
        ),
    )


@blueprint.route("/admin/indieauth", methods=["GET"])
@login_required
def admin_indieauth() -> _Response:
    return render_template(
        "admin_indieauth.html",
        indieauth_actions=DB.indieauth.find().sort("ts", -1).limit(100),
    )


@blueprint.route("/admin/tasks", methods=["GET"])
@login_required
def admin_tasks() -> _Response:
    return render_template(
        "admin_tasks.html",
        success=p.get_success(),
        dead=p.get_dead(),
        waiting=p.get_waiting(),
        cron=p.get_cron(),
    )


@blueprint.route("/admin/lookup", methods=["GET"])
@login_required
def admin_lookup() -> _Response:
    data = None
    meta = None
    if request.args.get("url"):
        data = lookup(request.args.get("url"))  # type: ignore
        if data:
            if data.has_type(ap.ActivityType.ANNOUNCE):
                meta = dict(
                    object=data.get_object().to_dict(),
                    object_actor=data.get_object().get_actor().to_dict(),
                    actor=data.get_actor().to_dict(),
                )

            elif data.has_type(ap.ActivityType.QUESTION):
                p.push(data.id, "/task/fetch_remote_question")

        print(data)
        app.logger.debug(data.to_dict())
    return render_template(
        "lookup.html", data=data, meta=meta, url=request.args.get("url")
    )


@blueprint.route("/admin/thread")
@login_required
def admin_thread() -> _Response:
    oid = request.args.get("oid")
    if not oid:
        abort(404)

    data = find_one_activity({**by_type(ap.ActivityType.CREATE), **by_object_id(oid)})

    if not data:
        abort(404)
    if data["meta"].get("deleted", False):
        abort(410)
    thread = _build_thread(data)

    tpl = "note.html"
    if request.args.get("debug"):
        tpl = "note_debug.html"
    return render_template(tpl, thread=thread, note=data)


@blueprint.route("/admin/new", methods=["GET"])
@login_required
def admin_new() -> _Response:
    reply_id = None
    content = ""
    thread: List[Any] = []
    print(request.args)
    if request.args.get("reply"):
        data = DB.activities.find_one({"activity.object.id": request.args.get("reply")})
        if data:
            reply = ap.parse_activity(data["activity"])
        else:
            data = dict(
                meta={},
                activity=dict(
                    object=ap.get_backend().fetch_iri(request.args.get("reply"))
                ),
            )
            reply = ap.parse_activity(data["activity"]["object"])

        reply_id = reply.id
        if reply.ACTIVITY_TYPE == ap.ActivityType.CREATE:
            reply_id = reply.get_object().id
        actor = reply.get_actor()
        domain = urlparse(actor.id).netloc
        # FIXME(tsileo): if reply of reply, fetch all participants
        content = f"@{actor.preferredUsername}@{domain} "
        thread = _build_thread(data)

    return render_template(
        "new.html",
        reply=reply_id,
        content=content,
        thread=thread,
        visibility=ap.Visibility,
        emojis=config.EMOJIS.split(" "),
    )


@blueprint.route("/admin/lists", methods=["GET"])
@login_required
def admin_lists() -> _Response:
    lists = list(DB.lists.find())

    return render_template("lists.html", lists=lists)


@blueprint.route("/admin/notifications")
@login_required
def admin_notifications() -> _Response:
    # Setup the cron for deleting old activities

    # FIXME(tsileo): put back to 12h
    p.push({}, "/task/cleanup", schedule="@every 1h")

    # Trigger a cleanup if asked
    if request.args.get("cleanup"):
        p.push({}, "/task/cleanup")

    # FIXME(tsileo): show unfollow (performed by the current actor) and liked???
    mentions_query = {
        "type": ap.ActivityType.CREATE.value,
        "activity.object.tag.type": "Mention",
        "activity.object.tag.name": f"@{config.USERNAME}@{config.DOMAIN}",
        "meta.deleted": False,
    }
    replies_query = {
        "type": ap.ActivityType.CREATE.value,
        "activity.object.inReplyTo": {"$regex": f"^{config.BASE_URL}"},
        "meta.poll_answer": False,
    }
    announced_query = {
        "type": ap.ActivityType.ANNOUNCE.value,
        "activity.object": {"$regex": f"^{config.BASE_URL}"},
    }
    new_followers_query = {"type": ap.ActivityType.FOLLOW.value}
    unfollow_query = {
        "type": ap.ActivityType.UNDO.value,
        "activity.object.type": ap.ActivityType.FOLLOW.value,
    }
    likes_query = {
        "type": ap.ActivityType.LIKE.value,
        "activity.object": {"$regex": f"^{config.BASE_URL}"},
    }
    followed_query = {"type": ap.ActivityType.ACCEPT.value}
    q = {
        "box": Box.INBOX.value,
        "$or": [
            mentions_query,
            announced_query,
            replies_query,
            new_followers_query,
            followed_query,
            unfollow_query,
            likes_query,
        ],
    }
    inbox_data, older_than, newer_than = paginated_query(DB.activities, q)
    if not newer_than:
        nstart = datetime.now(timezone.utc).isoformat()
    else:
        nstart = inbox_data[0]["_id"].generation_time.isoformat()
    if not older_than:
        nend = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    else:
        nend = inbox_data[-1]["_id"].generation_time.isoformat()
    print(nstart, nend)
    notifs = list(
        DB.notifications.find({"datetime": {"$lte": nstart, "$gt": nend}})
        .sort("_id", -1)
        .limit(50)
    )
    print(inbox_data)

    nid = None
    if inbox_data:
        nid = inbox_data[0]["_id"]

    inbox_data.extend(notifs)
    inbox_data = sorted(
        inbox_data, reverse=True, key=lambda doc: doc["_id"].generation_time
    )

    return render_template(
        "stream.html",
        inbox_data=inbox_data,
        older_than=older_than,
        newer_than=newer_than,
        nid=nid,
    )


@blueprint.route("/admin/stream")
@login_required
def admin_stream() -> _Response:
    q = {"meta.stream": True, "meta.deleted": False}

    tpl = "stream.html"
    if request.args.get("debug"):
        tpl = "stream_debug.html"
        if request.args.get("debug_inbox"):
            q = {}

    inbox_data, older_than, newer_than = paginated_query(
        DB.activities, q, limit=int(request.args.get("limit", 25))
    )

    return render_template(
        tpl, inbox_data=inbox_data, older_than=older_than, newer_than=newer_than
    )


@blueprint.route("/admin/list/<name>")
@login_required
def admin_list(name: str) -> _Response:
    list_ = DB.lists.find_one({"name": name})
    if not list_:
        abort(404)

    q = {
        "meta.stream": True,
        "meta.deleted": False,
        "meta.actor_id": {"$in": list_["members"]},
    }

    tpl = "stream.html"
    if request.args.get("debug"):
        tpl = "stream_debug.html"
        if request.args.get("debug_inbox"):
            q = {}

    inbox_data, older_than, newer_than = paginated_query(
        DB.activities, q, limit=int(request.args.get("limit", 25))
    )

    return render_template(
        tpl, inbox_data=inbox_data, older_than=older_than, newer_than=newer_than
    )


@blueprint.route("/admin/bookmarks")
@login_required
def admin_bookmarks() -> _Response:
    q = {"meta.bookmarked": True}

    tpl = "stream.html"
    if request.args.get("debug"):
        tpl = "stream_debug.html"
        if request.args.get("debug_inbox"):
            q = {}

    inbox_data, older_than, newer_than = paginated_query(
        DB.activities, q, limit=int(request.args.get("limit", 25))
    )

    return render_template(
        tpl, inbox_data=inbox_data, older_than=older_than, newer_than=newer_than
    )


@blueprint.route("/u2f/register", methods=["GET", "POST"])
@login_required
def u2f_register():
    # TODO(tsileo): ensure no duplicates
    if request.method == "GET":
        payload = u2f.begin_registration(ID)
        session["challenge"] = payload
        return render_template("u2f.html", payload=payload)
    else:
        resp = json.loads(request.form.get("resp"))
        device, device_cert = u2f.complete_registration(session["challenge"], resp)
        session["challenge"] = None
        DB.u2f.insert_one({"device": device, "cert": device_cert})
        session["logged_in"] = False
        return redirect("/login")


@blueprint.route("/authorize_follow", methods=["GET", "POST"])
@login_required
def authorize_follow():
    if request.method == "GET":
        return render_template(
            "authorize_remote_follow.html", profile=request.args.get("profile")
        )

    actor = get_actor_url(request.form.get("profile"))
    if not actor:
        abort(500)

    q = {
        "box": Box.OUTBOX.value,
        "type": ap.ActivityType.FOLLOW.value,
        "meta.undo": False,
        "activity.object": actor,
    }
    if DB.activities.count(q) > 0:
        return redirect("/following")

    follow = ap.Follow(
        actor=MY_PERSON.id, object=actor, to=[actor], cc=[ap.AS_PUBLIC], published=now()
    )
    post_to_outbox(follow)

    return redirect("/following")
