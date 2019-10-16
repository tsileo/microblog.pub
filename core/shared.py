import gzip
import json
import os
from functools import lru_cache
from functools import wraps
from typing import Any

import flask
from bson.objectid import ObjectId
from flask import Response
from flask import current_app as app
from flask import redirect
from flask import request
from flask import session
from flask import url_for
from flask_wtf.csrf import CSRFProtect
from little_boxes import activitypub as ap
from poussetaches import PousseTaches

import config
from config import DB
from config import ME
from core import activitypub
from core.db import find_activities
from core.meta import MetaKey
from core.meta import by_object_id
from core.meta import by_type
from core.meta import flag
from core.meta import not_deleted

# _Response = Union[flask.Response, werkzeug.wrappers.Response, str, Any]
_Response = Any

p = PousseTaches(
    os.getenv("MICROBLOGPUB_POUSSETACHES_HOST", "http://localhost:7991"),
    os.getenv("MICROBLOGPUB_INTERNAL_HOST", "http://localhost:5000"),
)


csrf = CSRFProtect()


back = activitypub.MicroblogPubBackend()
ap.use_backend(back)

MY_PERSON = ap.Person(**ME)


@lru_cache(512)
def build_resp(resp):
    """Encode the response to gzip if supported by the client."""
    headers = {"Cache-Control": "max-age=0, private, must-revalidate"}
    accept_encoding = request.headers.get("Accept-Encoding", "")
    if "gzip" in accept_encoding.lower():
        return (
            gzip.compress(resp.encode(), compresslevel=6),
            {**headers, "Vary": "Accept-Encoding", "Content-Encoding": "gzip"},
        )

    return resp, headers


def jsonify(data, content_type="application/json"):
    resp, headers = build_resp(json.dumps(data))
    return Response(headers={**headers, "Content-Type": content_type}, response=resp)


def htmlify(data):
    resp, headers = build_resp(data)
    return Response(
        response=resp, headers={**headers, "Content-Type": "text/html; charset=utf-8"}
    )


def activitypubify(**data):
    if "@context" not in data:
        data["@context"] = config.DEFAULT_CTX
    resp, headers = build_resp(json.dumps(data))
    return Response(
        response=resp, headers={**headers, "Content-Type": "application/activity+json"}
    )


def is_api_request():
    h = request.headers.get("Accept")
    if h is None:
        return False
    h = h.split(",")[0]
    if h in config.HEADERS or h == "application/json":
        return True
    return False


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
            return redirect(url_for("admin.admin_login", next=request.url))
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


def _build_thread(data, include_children=True, query=None):  # noqa: C901
    if query is None:
        query = {}
    data["_requested"] = True
    app.logger.info(f"_build_thread({data!r})")
    root_id = (
        data["meta"].get(MetaKey.THREAD_ROOT_PARENT.value)
        or data["meta"].get(MetaKey.OBJECT_ID.value)
        or data["remote_id"]
    )

    replies = [data]
    for dat in find_activities(
        {
            **by_object_id(root_id),
            **not_deleted(),
            **by_type(ap.ActivityType.CREATE),
            **query,
        }
    ):
        replies.append(dat)

    for dat in find_activities(
        {
            **flag(MetaKey.THREAD_ROOT_PARENT, root_id),
            **not_deleted(),
            **by_type(ap.ActivityType.CREATE),
            **query,
        }
    ):
        replies.append(dat)

    for dat in DB.replies.find(
        {**flag(MetaKey.THREAD_ROOT_PARENT, root_id), **not_deleted(), **query}
    ):
        # Make a Note/Question/... looks like a Create
        dat["meta"].update(
            {MetaKey.OBJECT_VISIBILITY.value: dat["meta"][MetaKey.VISIBILITY.value]}
        )
        dat = {
            "activity": {"object": dat["activity"]},
            "meta": dat["meta"],
            "_id": dat["_id"],
        }
        replies.append(dat)

    replies = sorted(replies, key=lambda d: d["meta"]["published"])

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
