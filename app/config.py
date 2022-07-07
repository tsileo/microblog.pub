import os
import subprocess
from pathlib import Path

import bcrypt
import pydantic
import tomli
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from itsdangerous import TimedSerializer
from itsdangerous import TimestampSigner

from app.utils.emoji import _load_emojis

ROOT_DIR = Path().parent.resolve()

_CONFIG_FILE = os.getenv("MICROBLOGPUB_CONFIG_FILE", "profile.toml")

VERSION_COMMIT = (
    subprocess.check_output(["git", "rev-parse", "--short=8", "HEAD"])
    .split()[0]
    .decode()
)

VERSION = f"2.0.0+{VERSION_COMMIT}"
USER_AGENT = f"microblogpub/{VERSION}"
AP_CONTENT_TYPE = "application/activity+json"


class Config(pydantic.BaseModel):
    domain: str
    username: str
    admin_password: bytes
    name: str
    summary: str
    https: bool
    icon_url: str
    secret: str
    debug: bool = False

    # Config items to make tests easier
    sqlalchemy_database: str | None = None
    key_path: str | None = None


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
USERNAME = CONFIG.username
BASE_URL = ID
DEBUG = CONFIG.debug
DB_PATH = CONFIG.sqlalchemy_database or ROOT_DIR / "data" / "microblogpub.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"
KEY_PATH = (
    (ROOT_DIR / CONFIG.key_path) if CONFIG.key_path else ROOT_DIR / "data" / "key.pem"
)
EMOJIS = "😺 😸 😹 😻 😼 😽 🙀 😿 😾"
# Emoji template for the FE
EMOJI_TPL = '<img src="/static/twemoji/{filename}.svg" alt="{raw}" class="emoji">'

_load_emojis(ROOT_DIR, BASE_URL)


session_serializer = TimedSerializer(CONFIG.secret, salt="microblogpub.login")
csrf_signer = TimestampSigner(
    os.urandom(16).hex(),
    salt=os.urandom(16).hex(),
)


def generate_csrf_token() -> str:
    return csrf_signer.sign(os.urandom(16).hex()).decode()


def verify_csrf_token(csrf_token: str = Form()) -> None:
    if not csrf_signer.validate(csrf_token, max_age=600):
        raise HTTPException(status_code=403, detail="CSRF error")
    return None
