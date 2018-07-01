import os
import subprocess
from datetime import datetime
from enum import Enum

import requests
import sass
import yaml
from itsdangerous import JSONWebSignatureSerializer
from pymongo import MongoClient

from little_boxes import strtobool
from utils.key import KEY_DIR
from utils.key import get_key
from utils.key import get_secret_key


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


CTX_AS = "https://www.w3.org/ns/activitystreams"
CTX_SECURITY = "https://w3id.org/security/v1"
AS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"
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
    PUBLIC_INSTANCES = conf.get("public_instances", [])

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
    CSS = sass.compile(string=raw_css)


USER_AGENT = (
    f"{requests.utils.default_user_agent()} (microblog.pub/{VERSION}; +{BASE_URL})"
)

mongo_client = MongoClient(
    host=[os.getenv("MICROBLOGPUB_MONGODB_HOST", "localhost:27017")]
)

DB_NAME = "{}_{}".format(USERNAME, DOMAIN.replace(".", "_"))
DB = mongo_client[DB_NAME]


def _drop_db():
    if not DEBUG_MODE:
        return

    mongo_client.drop_database(DB_NAME)


KEY = get_key(ID, USERNAME, DOMAIN)


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
    "@context": [CTX_AS, CTX_SECURITY],
    "type": "Person",
    "id": ID,
    "following": ID + "/following",
    "followers": ID + "/followers",
    "liked": ID + "/liked",
    "inbox": ID + "/inbox",
    "outbox": ID + "/outbox",
    "preferredUsername": USERNAME,
    "name": NAME,
    "summary": SUMMARY,
    "endpoints": {},
    "url": ID,
    "icon": {"mediaType": "image/png", "type": "Image", "url": ICON_URL},
    "publicKey": KEY.to_dict(),
}
