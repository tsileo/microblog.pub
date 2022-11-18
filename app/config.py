import hashlib
import hmac
import os
import secrets
from pathlib import Path

import bcrypt
import itsdangerous
import pydantic
import tomli
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from itsdangerous import URLSafeTimedSerializer
from loguru import logger
from mistletoe import markdown  # type: ignore

from app.customization import _CUSTOM_ROUTES
from app.utils.emoji import _load_emojis
from app.utils.version import get_version_commit

ROOT_DIR = Path().parent.resolve()

_CONFIG_FILE = os.getenv("MICROBLOGPUB_CONFIG_FILE", "profile.toml")

VERSION_COMMIT = "dev"

try:
    from app._version import VERSION_COMMIT  # type: ignore
except ImportError:
    VERSION_COMMIT = get_version_commit()

# Force reloading cache when the CSS is updated
CSS_HASH = "none"
try:
    css_data = (ROOT_DIR / "app" / "static" / "css" / "main.css").read_bytes()
    CSS_HASH = hashlib.md5(css_data, usedforsecurity=False).hexdigest()
except FileNotFoundError:
    pass

# Force reloading cache when the JS is changed
JS_HASH = "none"
try:
    # To keep things simple, we keep a single hash for the 2 files
    js_data_common = (ROOT_DIR / "app" / "static" / "common-admin.js").read_bytes()
    js_data_new = (ROOT_DIR / "app" / "static" / "new.js").read_bytes()
    JS_HASH = hashlib.md5(
        js_data_common + js_data_new, usedforsecurity=False
    ).hexdigest()
except FileNotFoundError:
    pass

MOVED_TO_FILE = ROOT_DIR / "data" / "moved_to.dat"


def _get_moved_to() -> str | None:
    if not MOVED_TO_FILE.exists():
        return None

    return MOVED_TO_FILE.read_text()


def set_moved_to(moved_to: str) -> None:
    MOVED_TO_FILE.write_text(moved_to)


VERSION = f"2.0.0+{VERSION_COMMIT}"
USER_AGENT = f"microblogpub/{VERSION}"
AP_CONTENT_TYPE = "application/activity+json"


class _PrivacyReplace(pydantic.BaseModel):
    domain: str
    replace_by: str


class _ProfileMetadata(pydantic.BaseModel):
    key: str
    value: str


class _BlockedServer(pydantic.BaseModel):
    hostname: str
    reason: str | None = None


class Config(pydantic.BaseModel):
    domain: str
    username: str
    admin_password: bytes
    name: str
    summary: str
    https: bool
    icon_url: str | None = None
    image_url: str | None = None
    secret: str
    debug: bool = False
    trusted_hosts: list[str] = ["127.0.0.1"]
    manually_approves_followers: bool = False
    privacy_replace: list[_PrivacyReplace] | None = None
    metadata: list[_ProfileMetadata] | None = None
    code_highlighting_theme = "friendly_grayscale"
    blocked_servers: list[_BlockedServer] = []
    custom_footer: str | None = None
    emoji: str | None = None
    also_known_as: str | None = None

    hides_followers: bool = False
    hides_following: bool = False

    inbox_retention_days: int = 15

    custom_content_security_policy: str | None = None

    # Config items to make tests easier
    sqlalchemy_database: str | None = None
    key_path: str | None = None

    # Only set when the app is served on a non-root path
    id: str | None = None


def load_config() -> Config:
    try:
        return Config.parse_obj(
            tomli.loads((ROOT_DIR / "data" / _CONFIG_FILE).read_text())
        )
    except FileNotFoundError:
        raise ValueError(
            f"Please run the configuration wizard, {_CONFIG_FILE} is missing"
        )


def is_activitypub_requested(req: Request) -> bool:
    accept_value = req.headers.get("accept")
    if not accept_value:
        return False
    for val in {
        "application/ld+json",
        "application/activity+json",
    }:
        if accept_value.startswith(val):
            return True

    return False


def verify_password(pwd: str) -> bool:
    return bcrypt.checkpw(pwd.encode(), CONFIG.admin_password)


CONFIG = load_config()
DOMAIN = CONFIG.domain
_SCHEME = "https" if CONFIG.https else "http"
ID = f"{_SCHEME}://{DOMAIN}"

# When running the app on a path, the ID maybe set by the config, but in this
# case, a valid webfinger must be served on the root domain
if CONFIG.id:
    ID = CONFIG.id
USERNAME = CONFIG.username
MANUALLY_APPROVES_FOLLOWERS = CONFIG.manually_approves_followers
HIDES_FOLLOWERS = CONFIG.hides_followers
HIDES_FOLLOWING = CONFIG.hides_following
PRIVACY_REPLACE = None
if CONFIG.privacy_replace:
    PRIVACY_REPLACE = {pr.domain: pr.replace_by for pr in CONFIG.privacy_replace}

BLOCKED_SERVERS = {blocked_server.hostname for blocked_server in CONFIG.blocked_servers}
ALSO_KNOWN_AS = CONFIG.also_known_as
CUSTOM_CONTENT_SECURITY_POLICY = CONFIG.custom_content_security_policy

INBOX_RETENTION_DAYS = CONFIG.inbox_retention_days
CUSTOM_FOOTER = (
    markdown(CONFIG.custom_footer.replace("{version}", VERSION))
    if CONFIG.custom_footer
    else None
)

BASE_URL = ID
DEBUG = CONFIG.debug
DB_PATH = CONFIG.sqlalchemy_database or ROOT_DIR / "data" / "microblogpub.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"
KEY_PATH = (
    (ROOT_DIR / CONFIG.key_path) if CONFIG.key_path else ROOT_DIR / "data" / "key.pem"
)
EMOJIS = "😺 😸 😹 😻 😼 😽 🙀 😿 😾"
if CONFIG.emoji:
    EMOJIS = CONFIG.emoji

# Emoji template for the FE
EMOJI_TPL = (
    '<img src="{base_url}/static/twemoji/{filename}.svg" alt="{raw}" class="emoji">'
)

_load_emojis(ROOT_DIR, BASE_URL)

CODE_HIGHLIGHTING_THEME = CONFIG.code_highlighting_theme

MOVED_TO = _get_moved_to()


_NavBarItem = tuple[str, str]


class NavBarItems:
    EXTRA_NAVBAR_ITEMS: list[_NavBarItem] = []
    INDEX_NAVBAR_ITEM: _NavBarItem | None = None
    NOTES_PATH = "/"


def load_custom_routes() -> None:
    try:
        from data import custom_routes  # type: ignore  # noqa: F401
    except ImportError:
        pass

    for path, custom_handler in _CUSTOM_ROUTES.items():
        # If a handler wants to replace the root, move the index to /notes
        if path == "/":
            NavBarItems.NOTES_PATH = "/notes"
            NavBarItems.INDEX_NAVBAR_ITEM = (path, custom_handler.title)
        else:
            if custom_handler.show_in_navbar:
                NavBarItems.EXTRA_NAVBAR_ITEMS.append((path, custom_handler.title))


session_serializer = URLSafeTimedSerializer(
    CONFIG.secret,
    salt=f"{ID}.session",
)
csrf_serializer = URLSafeTimedSerializer(
    CONFIG.secret,
    salt=f"{ID}.csrf",
)


def generate_csrf_token() -> str:
    return csrf_serializer.dumps(secrets.token_hex(16))  # type: ignore


def verify_csrf_token(
    csrf_token: str = Form(),
    redirect_url: str | None = Form(None),
) -> None:
    please_try_again = "please try again"
    if redirect_url:
        please_try_again = f'<a href="{redirect_url}">please try again</a>'
    try:
        csrf_serializer.loads(csrf_token, max_age=1800)
    except (itsdangerous.BadData, itsdangerous.SignatureExpired):
        logger.exception("Failed to verify CSRF token")
        raise HTTPException(
            status_code=403,
            detail=f"The security token has expired, {please_try_again}",
        )
    return None


def hmac_sha256() -> hmac.HMAC:
    return hmac.new(CONFIG.secret.encode(), digestmod=hashlib.sha256)
