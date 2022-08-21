"""Basic wizard for setting up microblog.pub configuration files."""
import os
import sys
from pathlib import Path
from typing import Any

import bcrypt
import tomli_w
from markdown import markdown  # type: ignore

from app.key import generate_key

_ROOT_DIR = Path().parent.parent.resolve()
_KEY_PATH = _ROOT_DIR / "data" / "key.pem"
_CONFIG_PATH = _ROOT_DIR / "data" / "profile.toml"


def setup_config_file(
    domain: str,
    username: str,
    name: str,
    summary: str,
    password: str,
) -> None:
    print("Generating microblog.pub config\n")
    if _KEY_PATH.exists():
        sys.exit(2)

    generate_key(_KEY_PATH)

    config_file = _CONFIG_PATH

    if config_file.exists():
        # Spit out the relative path for the "config artifacts"
        rconfig_file = "data/profile.toml"
        print(
            f"Existing setup detected, please delete {rconfig_file} "
            "before restarting the wizard"
        )
        sys.exit(2)

    dat: dict[str, Any] = {}
    dat["domain"] = domain
    dat["username"] = username
    dat["admin_password"] = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    dat["name"] = name
    dat["summary"] = markdown(summary)
    dat["https"] = True
    proto = "https"
    dat["icon_url"] = f'{proto}://{dat["domain"]}/static/nopic.png'
    dat["secret"] = os.urandom(16).hex()

    with config_file.open("w") as f:
        f.write(tomli_w.dumps(dat))

    print("Done")
    sys.exit(0)
