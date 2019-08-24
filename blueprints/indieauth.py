import binascii
import os
from datetime import datetime
from datetime import timedelta
from urllib.parse import urlencode

import flask
import mf2py
from flask import Response
from flask import abort
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from itsdangerous import BadSignature

from config import DB
from config import JWT
from core.shared import _get_ip
from core.shared import htmlify
from core.shared import jsonify
from core.shared import login_required

blueprint = flask.Blueprint("indieauth", __name__)


def build_auth_resp(payload):
    if request.headers.get("Accept") == "application/json":
        return jsonify(payload)
    return Response(
        status=200,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        response=urlencode(payload),
    )


def _get_prop(props, name, default=None):
    if name in props:
        items = props.get(name)
        if isinstance(items, list):
            return items[0]
        return items
    return default


def get_client_id_data(url):
    # FIXME(tsileo): ensure not localhost via `little_boxes.urlutils.is_url_valid`
    data = mf2py.parse(url=url)
    for item in data["items"]:
        if "h-x-app" in item["type"] or "h-app" in item["type"]:
            props = item.get("properties", {})
            print(props)
            return dict(
                logo=_get_prop(props, "logo"),
                name=_get_prop(props, "name"),
                url=_get_prop(props, "url"),
            )
    return dict(logo=None, name=url, url=url)


@blueprint.route("/indieauth/flow", methods=["POST"])
@login_required
def indieauth_flow():
    auth = dict(
        scope=" ".join(request.form.getlist("scopes")),
        me=request.form.get("me"),
        client_id=request.form.get("client_id"),
        state=request.form.get("state"),
        redirect_uri=request.form.get("redirect_uri"),
        response_type=request.form.get("response_type"),
        ts=datetime.now().timestamp(),
        code=binascii.hexlify(os.urandom(8)).decode("utf-8"),
        verified=False,
    )

    # XXX(tsileo): a whitelist for me values?

    # TODO(tsileo): redirect_uri checks
    if not auth["redirect_uri"]:
        abort(400)

    DB.indieauth.insert_one(auth)

    # FIXME(tsileo): fetch client ID and validate redirect_uri
    red = f'{auth["redirect_uri"]}?code={auth["code"]}&state={auth["state"]}&me={auth["me"]}'
    return redirect(red)


@blueprint.route("/indieauth", methods=["GET", "POST"])
def indieauth_endpoint():
    if request.method == "GET":
        if not session.get("logged_in"):
            return redirect(url_for("admin_login", next=request.url))

        me = request.args.get("me")
        # FIXME(tsileo): ensure me == ID
        client_id = request.args.get("client_id")
        redirect_uri = request.args.get("redirect_uri")
        state = request.args.get("state", "")
        response_type = request.args.get("response_type", "id")
        scope = request.args.get("scope", "").split()

        print("STATE", state)
        return htmlify(
            render_template(
                "indieauth_flow.html",
                client=get_client_id_data(client_id),
                scopes=scope,
                redirect_uri=redirect_uri,
                state=state,
                response_type=response_type,
                client_id=client_id,
                me=me,
            )
        )

    # Auth verification via POST
    code = request.form.get("code")
    redirect_uri = request.form.get("redirect_uri")
    client_id = request.form.get("client_id")

    ip, geoip = _get_ip()

    auth = DB.indieauth.find_one_and_update(
        {
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "verified": False,
        },
        {
            "$set": {
                "verified": True,
                "verified_by": "id",
                "verified_at": datetime.now().timestamp(),
                "ip_address": ip,
                "geoip": geoip,
            }
        },
    )
    print(auth)
    print(code, redirect_uri, client_id)

    # Ensure the code is recent
    if (datetime.now() - datetime.fromtimestamp(auth["ts"])) > timedelta(minutes=5):
        abort(400)

    if not auth:
        abort(403)
        return

    session["logged_in"] = True
    me = auth["me"]
    state = auth["state"]
    scope = auth["scope"]
    print("STATE", state)
    return build_auth_resp({"me": me, "state": state, "scope": scope})


@blueprint.route("/token", methods=["GET", "POST"])
def token_endpoint():
    # Generate a new token with the returned access code
    if request.method == "POST":
        code = request.form.get("code")
        me = request.form.get("me")
        redirect_uri = request.form.get("redirect_uri")
        client_id = request.form.get("client_id")

        now = datetime.now()
        ip, geoip = _get_ip()

        # This query ensure code, client_id, redirect_uri and me are matching with the code request
        auth = DB.indieauth.find_one_and_update(
            {
                "code": code,
                "me": me,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "verified": False,
            },
            {
                "$set": {
                    "verified": True,
                    "verified_by": "code",
                    "verified_at": now.timestamp(),
                    "ip_address": ip,
                    "geoip": geoip,
                }
            },
        )

        if not auth:
            abort(403)

        scope = auth["scope"].split()

        # Ensure there's at least one scope
        if not len(scope):
            abort(400)

        # Ensure the code is recent
        if (now - datetime.fromtimestamp(auth["ts"])) > timedelta(minutes=5):
            abort(400)

        payload = dict(me=me, client_id=client_id, scope=scope, ts=now.timestamp())
        token = JWT.dumps(payload).decode("utf-8")
        DB.indieauth.update_one(
            {"_id": auth["_id"]},
            {
                "$set": {
                    "token": token,
                    "token_expires": (now + timedelta(minutes=30)).timestamp(),
                }
            },
        )

        return build_auth_resp(
            {"me": me, "scope": auth["scope"], "access_token": token}
        )

    # Token verification
    token = request.headers.get("Authorization").replace("Bearer ", "")
    try:
        payload = JWT.loads(token)
    except BadSignature:
        abort(403)

    # Check the token expritation (valid for 3 hours)
    if (datetime.now() - datetime.fromtimestamp(payload["ts"])) > timedelta(
        minutes=180
    ):
        abort(401)

    return build_auth_resp(
        {
            "me": payload["me"],
            "scope": " ".join(payload["scope"]),
            "client_id": payload["client_id"],
        }
    )
