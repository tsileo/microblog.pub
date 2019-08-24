import mimetypes
from typing import Any

import flask
from flask import abort
from flask import request
from little_boxes import activitypub as ap

import config
from config import DB
from core.meta import Box
from core.shared import jsonify

blueprint = flask.Blueprint("well_known", __name__)


@blueprint.route("/.well-known/webfinger")
def wellknown_webfinger() -> Any:
    """Exposes/servers WebFinger data."""
    resource = request.args.get("resource")
    if resource not in [f"acct:{config.USERNAME}@{config.DOMAIN}", config.ID]:
        abort(404)

    out = {
        "subject": f"acct:{config.USERNAME}@{config.DOMAIN}",
        "aliases": [config.ID],
        "links": [
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": config.ID,
            },
            {"rel": "self", "type": "application/activity+json", "href": config.ID},
            {
                "rel": "http://ostatus.org/schema/1.0/subscribe",
                "template": config.BASE_URL + "/authorize_follow?profile={uri}",
            },
            {"rel": "magic-public-key", "href": config.KEY.to_magic_key()},
            {
                "href": config.ICON_URL,
                "rel": "http://webfinger.net/rel/avatar",
                "type": mimetypes.guess_type(config.ICON_URL)[0],
            },
        ],
    }

    return jsonify(out, "application/jrd+json; charset=utf-8")


@blueprint.route("/.well-known/nodeinfo")
def wellknown_nodeinfo() -> Any:
    """Exposes the NodeInfo endpoint (http://nodeinfo.diaspora.software/)."""
    return jsonify(
        {
            "links": [
                {
                    "rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                    "href": f"{config.ID}/nodeinfo",
                }
            ]
        }
    )


@blueprint.route("/nodeinfo")
def nodeinfo() -> Any:
    """NodeInfo endpoint."""
    q = {
        "box": Box.OUTBOX.value,
        "meta.deleted": False,
        "type": {"$in": [ap.ActivityType.CREATE.value, ap.ActivityType.ANNOUNCE.value]},
    }

    out = {
        "version": "2.1",
        "software": {
            "name": "microblogpub",
            "version": config.VERSION,
            "repository": "https://github.com/tsileo/microblog.pub",
        },
        "protocols": ["activitypub"],
        "services": {"inbound": [], "outbound": []},
        "openRegistrations": False,
        "usage": {"users": {"total": 1}, "localPosts": DB.activities.count(q)},
        "metadata": {
            "nodeName": f"@{config.USERNAME}@{config.DOMAIN}",
            "version": config.VERSION,
            "versionDate": config.VERSION_DATE,
        },
    }

    return jsonify(
        out,
        "application/json; profile=http://nodeinfo.diaspora.software/ns/schema/2.1#",
    )
