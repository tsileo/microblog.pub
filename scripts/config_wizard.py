"""Basic wizard for setting up microblog.pub configuration files."""
import os
import sys
from pathlib import Path
from typing import Any

import bcrypt
import tomli_w
from markdown import markdown  # type: ignore
from prompt_toolkit import prompt

from app.key import generate_key

_ROOT_DIR = Path().parent.resolve()
_KEY_PATH = _ROOT_DIR / "data" / "key.pem"


def main() -> None:
    print("Welcome to microblog.pub setup wizard\n")
    print("Generating key...")
    if _KEY_PATH.exists():
        yn = ""
        while yn not in ["y", "n"]:
            yn = prompt(
                "WARNING, a key already exists, overwrite it? (y/n): ", default="n"
            ).lower()
            if yn == "y":
                generate_key(_KEY_PATH)
    else:
        generate_key(_KEY_PATH)

    config_file = Path("data/profile.toml")

    if config_file.exists():
        # Spit out the relative path for the "config artifacts"
        rconfig_file = "data/profile.toml"
        print(
            f"Existing setup detected, please delete {rconfig_file} "
            "before restarting the wizard"
        )
        sys.exit(2)

    dat: dict[str, Any] = {}
    print("Your identity will be @{username}@{domain}")
    dat["domain"] = prompt("domain: ")
    dat["username"] = prompt("username: ")
    dat["admin_password"] = bcrypt.hashpw(
        prompt("admin password: ", is_password=True).encode(), bcrypt.gensalt()
    ).decode()
    dat["name"] = prompt("name (e.g. John Doe): ", default=dat["username"])
    dat["summary"] = markdown(
        prompt(
            (
                "summary (short description, in markdown, "
                "press [ESC] then [ENTER] to submit):\n"
            ),
            multiline=True,
        )
    )
    dat["https"] = True
    proto = "https"
    yn = ""
    while yn not in ["y", "n"]:
        yn = prompt("will the site be served via https? (y/n): ", default="y").lower()
    if yn == "n":
        dat["https"] = False
        proto = "http"

    print("Note that you can put your icon/avatar in the static/ directory")
    dat["icon_url"] = prompt(
        "icon URL: ", default=f'{proto}://{dat["domain"]}/static/nopic.png'
    )
    dat["secret"] = os.urandom(16).hex()

    with config_file.open("w") as f:
        f.write(tomli_w.dumps(dat))

    print("Done")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Aborted")
        sys.exit(1)
