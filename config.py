import mimetypes
import os
import subprocess
from datetime import datetime
from enum import Enum

import pymongo
import requests
import yaml
from itsdangerous import JSONWebSignatureSerializer
from little_boxes import strtobool
from little_boxes.activitypub import DEFAULT_CTX
from pymongo import MongoClient

import sass
from utils.key import KEY_DIR
from utils.key import get_key
from utils.key import get_secret_key
from utils.media import MediaCache


class ThemeStyle(Enum):
    LIGHT = "light"
    DARK = "dark"


DEFAULT_THEME_STYLE = ThemeStyle.LIGHT.value

DEFAULT_THEME_PRIMARY_COLOR = {
    ThemeStyle.LIGHT: "#1d781d",  # Green
    ThemeStyle.DARK: "#33ff00",  # Purple
}


def noop():
    pass


CUSTOM_CACHE_HOOKS = False
try:
    from cache_hooks import purge as custom_cache_purge_hook
except ModuleNotFoundError:
    custom_cache_purge_hook = noop

VERSION = (
    subprocess.check_output(["git", "describe", "--always"]).split()[0].decode("utf-8")
)

DEBUG_MODE = strtobool(os.getenv("MICROBLOGPUB_DEBUG", "false"))

HEADERS = [
    "application/activity+json",
    "application/ld+json;profile=https://www.w3.org/ns/activitystreams",
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
    "application/ld+json",
]


with open(os.path.join(KEY_DIR, "me.yml")) as f:
    conf = yaml.load(f)

    USERNAME = conf["username"]
    NAME = conf["name"]
    DOMAIN = conf["domain"]
    SCHEME = "https" if conf.get("https", True) else "http"
    BASE_URL = SCHEME + "://" + DOMAIN
    ID = BASE_URL
    SUMMARY = conf["summary"]
    ICON_URL = conf["icon_url"]
    PASS = conf["pass"]
    EXTRA_INBOXES = conf.get("extra_inboxes", [])

    HIDE_FOLLOWING = conf.get("hide_following", True)

    # Theme-related config
    theme_conf = conf.get("theme", {})
    THEME_STYLE = ThemeStyle(theme_conf.get("style", DEFAULT_THEME_STYLE))
    THEME_COLOR = theme_conf.get("color", DEFAULT_THEME_PRIMARY_COLOR[THEME_STYLE])


SASS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sass")
theme_css = f"$primary-color: {THEME_COLOR};\n"
with open(os.path.join(SASS_DIR, f"{THEME_STYLE.value}.scss")) as f:
    theme_css += f.read()
    theme_css += "\n"
with open(os.path.join(SASS_DIR, "base_theme.scss")) as f:
    raw_css = theme_css + f.read()
    CSS = sass.compile(string=raw_css, output_style="compressed")


USER_AGENT = (
    f"{requests.utils.default_user_agent()} (microblog.pub/{VERSION}; +{BASE_URL})"
)

mongo_client = MongoClient(
    host=[os.getenv("MICROBLOGPUB_MONGODB_HOST", "localhost:27017")]
)

DB_NAME = "{}_{}".format(USERNAME, DOMAIN.replace(".", "_"))
DB = mongo_client[DB_NAME]
GRIDFS = mongo_client[f"{DB_NAME}_gridfs"]
MEDIA_CACHE = MediaCache(GRIDFS, USER_AGENT)


def create_indexes():
    if "trash" not in DB.collection_names():
        DB.create_collection("trash", capped=True, size=50 << 20)  # 50 MB

    DB.command("compact", "activities")
    DB.activities.create_index([("remote_id", pymongo.ASCENDING)])
    DB.activities.create_index([("activity.object.id", pymongo.ASCENDING)])
    DB.activities.create_index([("meta.thread_root_parent", pymongo.ASCENDING)])
    DB.activities.create_index(
        [
            ("meta.thread_root_parent", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
        ]
    )
    DB.activities.create_index(
        [("activity.object.id", pymongo.ASCENDING), ("meta.deleted", pymongo.ASCENDING)]
    )
    DB.cache2.create_index(
        [
            ("path", pymongo.ASCENDING),
            ("type", pymongo.ASCENDING),
            ("arg", pymongo.ASCENDING),
        ]
    )
    DB.cache2.create_index("date", expireAfterSeconds=3600 * 12)

    # Index for the block query
    DB.activities.create_index(
        [
            ("box", pymongo.ASCENDING),
            ("type", pymongo.ASCENDING),
            ("meta.undo", pymongo.ASCENDING),
        ]
    )

    # Index for count queries
    DB.activities.create_index(
        [
            ("box", pymongo.ASCENDING),
            ("type", pymongo.ASCENDING),
            ("meta.undo", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
        ]
    )

    DB.activities.create_index([("box", pymongo.ASCENDING)])

    # Outbox query
    DB.activities.create_index(
        [
            ("box", pymongo.ASCENDING),
            ("type", pymongo.ASCENDING),
            ("meta.undo", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
            ("meta.public", pymongo.ASCENDING),
        ]
    )

    DB.activities.create_index(
        [
            ("type", pymongo.ASCENDING),
            ("activity.object.type", pymongo.ASCENDING),
            ("activity.object.inReplyTo", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
        ]
    )


def _drop_db():
    if not DEBUG_MODE:
        return

    mongo_client.drop_database(DB_NAME)


KEY = get_key(ID, ID + "#main-key", USERNAME, DOMAIN)


JWT_SECRET = get_secret_key("jwt")
JWT = JSONWebSignatureSerializer(JWT_SECRET)


def _admin_jwt_token() -> str:
    return JWT.dumps(  # type: ignore
        {"me": "ADMIN", "ts": datetime.now().timestamp()}
    ).decode(  # type: ignore
        "utf-8"
    )


ADMIN_API_KEY = get_secret_key("admin_api_key", _admin_jwt_token)

ME = {
    "@context": DEFAULT_CTX,
    "type": "Person",
    "id": ID,
    "following": ID + "/following",
    "followers": ID + "/followers",
    "featured": ID + "/featured",
    "liked": ID + "/liked",
    "inbox": ID + "/inbox",
    "outbox": ID + "/outbox",
    "preferredUsername": USERNAME,
    "name": NAME,
    "summary": SUMMARY,
    "endpoints": {},
    "url": ID,
    "manuallyApprovesFollowers": False,
    "attachment": [],
    "icon": {
        "mediaType": mimetypes.guess_type(ICON_URL)[0],
        "type": "Image",
        "url": ICON_URL,
    },
    "publicKey": KEY.to_dict(),
}

# TODO(tsileo): read the config from the YAML if set
EMOJIS = "😺 😸 😹 😻 😼 😽 🙀 😿 😾"
