"""Basic wizard for setting up microblog.pub configuration files."""
import binascii
import os
import sys
from pathlib import Path

import bcrypt
from markdown import markdown
from prompt_toolkit import prompt


def main() -> None:
    print("Welcome to microblog.pub setup wizard\n")

    config_file = Path("/app/out/config/me.yml")
    env_file = Path("/app/out/.env")

    if config_file.exists():
        # Spit out the relative path for the "config artifacts"
        rconfig_file = "config/me.yml"
        print(
            f"Existing setup detected, please delete {rconfig_file} before restarting the wizard"
        )
        sys.exit(2)

    dat = {}
    print("Your identity will be @{username}@{domain}")
    dat["domain"] = prompt("domain: ")
    dat["username"] = prompt("username: ")
    dat["pass"] = bcrypt.hashpw(
        prompt("password: ", is_password=True).encode(), bcrypt.gensalt()
    ).decode()
    dat["name"] = prompt("name (e.g. John Doe): ")
    dat["summary"] = markdown(
        prompt(
            "summary (short description, in markdown, press [ESC] then [ENTER] to submit):\n",
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

    dat["icon_url"] = prompt(
        "icon URL: ", default=f'{proto}://{dat["domain"]}/static/nopic.png'
    )

    out = ""
    for k, v in dat.items():
        out += f"{k}: {v!r}\n"

    with config_file.open("w") as f:
        f.write(out)

    proj_name = os.getenv("MICROBLOGPUB_WIZARD_PROJECT_NAME", "microblogpub")

    env = {
        "WEB_PORT": 5005,
        "CONFIG_DIR": "./config",
        "DATA_DIR": "./data",
        "POUSSETACHES_AUTH_KEY": binascii.hexlify(os.urandom(32)).decode(),
        "COMPOSE_PROJECT_NAME": proj_name,
    }

    out2 = ""
    for k, v in env.items():
        out2 += f"{k}={v}\n"

    with env_file.open("w") as f:
        f.write(out2)

    print("Done")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Aborted")
        sys.exit(1)
