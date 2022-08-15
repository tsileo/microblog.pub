import mf2py  # type: ignore

from app import activitypub as ap
from app import webfinger
from app.actor import Actor
from app.actor import RemoteActor
from app.ap_object import RemoteObject
from app.database import AsyncSession


async def lookup(db_session: AsyncSession, query: str) -> Actor | RemoteObject:
    if query.startswith("@"):
        query = await webfinger.get_actor_url(query)  # type: ignore  # None check below

        if not query:
            raise ap.NotAnObjectError(query)

    try:
        ap_obj = await ap.fetch(query)
    except ap.NotAnObjectError as not_an_object_error:
        resp = not_an_object_error.resp
        if not resp:
            raise ap.NotAnObjectError(query)

        alternate_obj = None
        if resp.headers.get("content-type", "").startswith("text/html"):
            for alternate in mf2py.parse(doc=resp.text).get("alternates", []):
                if alternate.get("type") == "application/activity+json":
                    alternate_obj = await ap.fetch(alternate["url"])

        if alternate_obj:
            ap_obj = alternate_obj
        else:
            raise

    if ap.as_list(ap_obj["type"])[0] in ap.ACTOR_TYPES:
        return RemoteActor(ap_obj)
    else:
        return await RemoteObject.from_raw_object(ap_obj)
