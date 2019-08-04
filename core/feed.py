from typing import Any
from typing import Dict
from typing import Optional

from feedgen.feed import FeedGenerator
from html2text import html2text
from little_boxes import activitypub as ap

from config import ID
from config import ME
from config import USERNAME
from core.db import DB
from core.meta import Box


def gen_feed():
    fg = FeedGenerator()
    fg.id(f"{ID}")
    fg.title(f"{USERNAME} notes")
    fg.author({"name": USERNAME, "email": "t@a4.io"})
    fg.link(href=ID, rel="alternate")
    fg.description(f"{USERNAME} notes")
    fg.logo(ME.get("icon", {}).get("url"))
    fg.language("en")
    for item in DB.activities.find(
        {
            "box": Box.OUTBOX.value,
            "type": "Create",
            "meta.deleted": False,
            "meta.public": True,
        },
        limit=10,
    ).sort("_id", -1):
        fe = fg.add_entry()
        fe.id(item["activity"]["object"].get("url"))
        fe.link(href=item["activity"]["object"].get("url"))
        fe.title(item["activity"]["object"]["content"])
        fe.description(item["activity"]["object"]["content"])
    return fg


def json_feed(path: str) -> Dict[str, Any]:
    """JSON Feed (https://jsonfeed.org/) document."""
    data = []
    for item in DB.activities.find(
        {
            "box": Box.OUTBOX.value,
            "type": "Create",
            "meta.deleted": False,
            "meta.public": True,
        },
        limit=10,
    ).sort("_id", -1):
        data.append(
            {
                "id": item["activity"]["id"],
                "url": item["activity"]["object"].get("url"),
                "content_html": item["activity"]["object"]["content"],
                "content_text": html2text(item["activity"]["object"]["content"]),
                "date_published": item["activity"]["object"].get("published"),
            }
        )
    return {
        "version": "https://jsonfeed.org/version/1",
        "user_comment": (
            "This is a microblog feed. You can add this to your feed reader using the following URL: "
            + ID
            + path
        ),
        "title": USERNAME,
        "home_page_url": ID,
        "feed_url": ID + path,
        "author": {
            "name": USERNAME,
            "url": ID,
            "avatar": ME.get("icon", {}).get("url"),
        },
        "items": data,
    }


def build_inbox_json_feed(
    path: str, request_cursor: Optional[str] = None
) -> Dict[str, Any]:
    """Build a JSON feed from the inbox activities."""
    data = []
    cursor = None

    q: Dict[str, Any] = {
        "type": "Create",
        "meta.deleted": False,
        "box": Box.INBOX.value,
    }
    if request_cursor:
        q["_id"] = {"$lt": request_cursor}

    for item in DB.activities.find(q, limit=50).sort("_id", -1):
        actor = ap.get_backend().fetch_iri(item["activity"]["actor"])
        data.append(
            {
                "id": item["activity"]["id"],
                "url": item["activity"]["object"].get("url"),
                "content_html": item["activity"]["object"]["content"],
                "content_text": html2text(item["activity"]["object"]["content"]),
                "date_published": item["activity"]["object"].get("published"),
                "author": {
                    "name": actor.get("name", actor.get("preferredUsername")),
                    "url": actor.get("url"),
                    "avatar": actor.get("icon", {}).get("url"),
                },
            }
        )
        cursor = str(item["_id"])

    resp = {
        "version": "https://jsonfeed.org/version/1",
        "title": f"{USERNAME}'s stream",
        "home_page_url": ID,
        "feed_url": ID + path,
        "items": data,
    }
    if cursor and len(data) == 50:
        resp["next_url"] = ID + path + "?cursor=" + cursor

    return resp
