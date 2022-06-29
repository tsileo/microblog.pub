import mf2py  # type: ignore

from app import activitypub as ap
from app import webfinger
from app.actor import Actor
from app.actor import fetch_actor
from app.ap_object import RemoteObject
from app.database import AsyncSession


async def lookup(db_session: AsyncSession, query: str) -> Actor | RemoteObject:
    if query.startswith("@"):
        query = webfinger.get_actor_url(query)  # type: ignore  # None check below

        if not query:
            raise ap.NotAnObjectError(query)

    try:
        ap_obj = ap.fetch(query)
    except ap.NotAnObjectError as not_an_object_error:
        resp = not_an_object_error.resp
        if not resp:
            raise ap.NotAnObjectError(query)

        alternate_obj = None
        if resp.headers.get("content-type", "").startswith("text/html"):
            for alternate in mf2py.parse(doc=resp.text).get("alternates", []):
                if alternate.get("type") == "application/activity+json":
                    alternate_obj = ap.fetch(alternate["url"])

        if alternate_obj:
            ap_obj = alternate_obj
        else:
            raise

    if ap_obj["type"] in ap.ACTOR_TYPES:
        actor = await fetch_actor(db_session, ap_obj["id"])
        return actor
    else:
        return RemoteObject(ap_obj)
