from functools import wraps

import flask
from flask import abort
from flask import current_app as app
from flask import redirect
from flask import request
from flask import session
from itsdangerous import BadSignature
from little_boxes import activitypub as ap
from little_boxes.errors import NotFromOutboxError

from app_utils import MY_PERSON
from app_utils import csrf
from app_utils import post_to_outbox
from config import ID
from config import JWT
from utils import now

api = flask.Blueprint("api", __name__)


def _api_required() -> None:
    if session.get("logged_in"):
        if request.method not in ["GET", "HEAD"]:
            # If a standard API request is made with a "login session", it must havw a CSRF token
            csrf.protect()
        return

    # Token verification
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        # IndieAuth token
        token = request.form.get("access_token", "")

    # Will raise a BadSignature on bad auth
    payload = JWT.loads(token)
    app.logger.info(f"api call by {payload}")


def api_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            _api_required()
        except BadSignature:
            abort(401)

        return f(*args, **kwargs)

    return decorated_function


def _user_api_arg(key: str, **kwargs):
    """Try to get the given key from the requests, try JSON body, form data and query arg."""
    if request.is_json:
        oid = request.json.get(key)
    else:
        oid = request.args.get(key) or request.form.get(key)

    if not oid:
        if "default" in kwargs:
            app.logger.info(f'{key}={kwargs.get("default")}')
            return kwargs.get("default")

        raise ValueError(f"missing {key}")

    app.logger.info(f"{key}={oid}")
    return oid


def _user_api_get_note(from_outbox: bool = False):
    oid = _user_api_arg("id")
    app.logger.info(f"fetching {oid}")
    note = ap.parse_activity(ap.get_backend().fetch_iri(oid))
    if from_outbox and not note.id.startswith(ID):
        raise NotFromOutboxError(
            f"cannot load {note.id}, id must be owned by the server"
        )

    return note


def _user_api_response(**kwargs):
    _redirect = _user_api_arg("redirect", default=None)
    if _redirect:
        return redirect(_redirect)

    resp = flask.jsonify(**kwargs)
    resp.status_code = 201
    return resp


@api.route("/note/delete", methods=["POST"])
@api_required
def api_delete():
    """API endpoint to delete a Note activity."""
    note = _user_api_get_note(from_outbox=True)

    # Create the delete, same audience as the Create object
    delete = ap.Delete(
        actor=ID,
        object=ap.Tombstone(id=note.id).to_dict(embed=True),
        to=note.to,
        cc=note.cc,
        published=now(),
    )

    delete_id = post_to_outbox(delete)

    return _user_api_response(activity=delete_id)


@api.route("/boost", methods=["POST"])
@api_required
def api_boost():
    note = _user_api_get_note()

    # Ensures the note visibility allow us to build an Announce (in respect to the post visibility)
    if ap.get_visibility(note) not in [ap.Visibility.PUBLIC, ap.Visibility.UNLISTED]:
        abort(400)

    announce = ap.Announce(
        actor=MY_PERSON.id,
        object=note.id,
        to=[MY_PERSON.followers, note.attributedTo],
        cc=[ap.AS_PUBLIC],
        published=now(),
    )
    announce_id = post_to_outbox(announce)

    return _user_api_response(activity=announce_id)
