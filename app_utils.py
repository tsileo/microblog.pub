import os
from datetime import datetime
from datetime import timezone
from functools import wraps
from typing import Any
from typing import Dict
from typing import Union

import flask
import werkzeug
from bson.objectid import ObjectId
from flask import current_app as app
from flask import redirect
from flask import request
from flask import session
from flask import url_for
from flask_wtf.csrf import CSRFProtect
from little_boxes import activitypub as ap
from little_boxes.activitypub import format_datetime
from poussetaches import PousseTaches

import activitypub
from activitypub import Box
from activitypub import _answer_key
from config import DB
from config import ME
from tasks import Tasks

_Response = Union[flask.Response, werkzeug.wrappers.Response, str]

p = PousseTaches(
    os.getenv("MICROBLOGPUB_POUSSETACHES_HOST", "http://localhost:7991"),
    os.getenv("MICROBLOGPUB_INTERNAL_HOST", "http://localhost:5000"),
)


csrf = CSRFProtect()


back = activitypub.MicroblogPubBackend()
ap.use_backend(back)

MY_PERSON = ap.Person(**ME)


def add_response_headers(headers={}):
    """This decorator adds the headers passed in to the response"""

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            resp = flask.make_response(f(*args, **kwargs))
            h = resp.headers
            for header, value in headers.items():
                h[header] = value
            return resp

        return decorated_function

    return decorator


def noindex(f):
    """This decorator passes X-Robots-Tag: noindex, nofollow"""
    return add_response_headers({"X-Robots-Tag": "noindex, nofollow"})(f)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("admin_login", next=request.url))
        return f(*args, **kwargs)

    return decorated_function


def _get_ip():
    """Guess the IP address from the request. Only used for security purpose (failed logins or bad payload).

    Geoip will be returned if the "broxy" headers are set (it does Geoip
    using an offline database and append these special headers).
    """
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    geoip = None
    if request.headers.get("Broxy-Geoip-Country"):
        geoip = (
            request.headers.get("Broxy-Geoip-Country")
            + "/"
            + request.headers.get("Broxy-Geoip-Region")
        )
    return ip, geoip


def post_to_outbox(activity: ap.BaseActivity) -> str:
    if activity.has_type(ap.CREATE_TYPES):
        activity = activity.build_create()

    # Assign create a random ID
    obj_id = back.random_object_id()

    activity.set_id(back.activity_url(obj_id), obj_id)

    back.save(Box.OUTBOX, activity)
    Tasks.cache_actor(activity.id)
    Tasks.finish_post_to_outbox(activity.id)
    return activity.id


def _build_thread(data, include_children=True):  # noqa: C901
    data["_requested"] = True
    app.logger.info(f"_build_thread({data!r})")
    root_id = data["meta"].get("thread_root_parent", data["activity"]["object"]["id"])

    query = {
        "$or": [{"meta.thread_root_parent": root_id}, {"activity.object.id": root_id}],
        "meta.deleted": False,
    }
    replies = [data]
    for dat in DB.activities.find(query):
        print(dat["type"])
        if dat["type"][0] == ap.ActivityType.CREATE.value:
            replies.append(dat)
        if dat["type"][0] == ap.ActivityType.UPDATE.value:
            continue
        else:
            # Make a Note/Question/... looks like a Create
            dat = {
                "activity": {"object": dat["activity"]},
                "meta": dat["meta"],
                "_id": dat["_id"],
            }
            replies.append(dat)

    replies = sorted(replies, key=lambda d: d["activity"]["object"]["published"])

    # Index all the IDs in order to build a tree
    idx = {}
    replies2 = []
    for rep in replies:
        rep_id = rep["activity"]["object"]["id"]
        if rep_id in idx:
            continue
        idx[rep_id] = rep.copy()
        idx[rep_id]["_nodes"] = []
        replies2.append(rep)

    # Build the tree
    for rep in replies2:
        rep_id = rep["activity"]["object"]["id"]
        if rep_id == root_id:
            continue
        reply_of = ap._get_id(rep["activity"]["object"].get("inReplyTo"))
        try:
            idx[reply_of]["_nodes"].append(rep)
        except KeyError:
            app.logger.info(f"{reply_of} is not there! skipping {rep}")

    # Flatten the tree
    thread = []

    def _flatten(node, level=0):
        node["_level"] = level
        thread.append(node)

        for snode in sorted(
            idx[node["activity"]["object"]["id"]]["_nodes"],
            key=lambda d: d["activity"]["object"]["published"],
        ):
            _flatten(snode, level=level + 1)

    try:
        _flatten(idx[root_id])
    except KeyError:
        app.logger.info(f"{root_id} is not there! skipping")

    return thread


def paginated_query(db, q, limit=25, sort_key="_id"):
    older_than = newer_than = None
    query_sort = -1
    first_page = not request.args.get("older_than") and not request.args.get(
        "newer_than"
    )

    query_older_than = request.args.get("older_than")
    query_newer_than = request.args.get("newer_than")

    if query_older_than:
        q["_id"] = {"$lt": ObjectId(query_older_than)}
    elif query_newer_than:
        q["_id"] = {"$gt": ObjectId(query_newer_than)}
        query_sort = 1

    outbox_data = list(db.find(q, limit=limit + 1).sort(sort_key, query_sort))
    outbox_len = len(outbox_data)
    outbox_data = sorted(
        outbox_data[:limit], key=lambda x: str(x[sort_key]), reverse=True
    )

    if query_older_than:
        newer_than = str(outbox_data[0]["_id"])
        if outbox_len == limit + 1:
            older_than = str(outbox_data[-1]["_id"])
    elif query_newer_than:
        older_than = str(outbox_data[-1]["_id"])
        if outbox_len == limit + 1:
            newer_than = str(outbox_data[0]["_id"])
    elif first_page and outbox_len == limit + 1:
        older_than = str(outbox_data[-1]["_id"])

    return outbox_data, older_than, newer_than


def _add_answers_to_question(raw_doc: Dict[str, Any]) -> None:
    activity = raw_doc["activity"]
    if (
        ap._has_type(activity["type"], ap.ActivityType.CREATE)
        and "object" in activity
        and ap._has_type(activity["object"]["type"], ap.ActivityType.QUESTION)
    ):
        for choice in activity["object"].get("oneOf", activity["object"].get("anyOf")):
            choice["replies"] = {
                "type": ap.ActivityType.COLLECTION.value,
                "totalItems": raw_doc["meta"]
                .get("question_answers", {})
                .get(_answer_key(choice["name"]), 0),
            }
        now = datetime.now(timezone.utc)
        if format_datetime(now) >= activity["object"]["endTime"]:
            activity["object"]["closed"] = activity["object"]["endTime"]
