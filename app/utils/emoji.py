import mimetypes
import re
import typing
from pathlib import Path

if typing.TYPE_CHECKING:
    from app.activitypub import RawObject

EMOJI_REGEX = re.compile(r"(:[\d\w]+:)")

EMOJIS: dict[str, "RawObject"] = {}
EMOJIS_BY_NAME: dict[str, "RawObject"] = {}


def _load_emojis(root_dir: Path, base_url: str) -> None:
    if EMOJIS:
        return
    for dir_name, path in (
        (root_dir / "app" / "static" / "emoji", "static/emoji"),
        (root_dir / "data" / "custom_emoji", "custom_emoji"),
    ):
        for emoji in dir_name.iterdir():
            mt = mimetypes.guess_type(emoji.name)[0]
            if mt and mt.startswith("image/"):
                name = emoji.name.split(".")[0]
                if not re.match(EMOJI_REGEX, f":{name}:"):
                    continue
                ap_emoji: "RawObject" = {
                    "type": "Emoji",
                    "name": f":{name}:",
                    "updated": "1970-01-01T00:00:00Z",  # XXX: we don't track date
                    "id": f"{base_url}/e/{name}",
                    "icon": {
                        "mediaType": mt,
                        "type": "Image",
                        "url": f"{base_url}/{path}/{emoji.name}",
                    },
                }
                EMOJIS[emoji.name] = ap_emoji
                EMOJIS_BY_NAME[ap_emoji["name"]] = ap_emoji


def tags(content: str) -> list["RawObject"]:
    tags = []
    added = set()
    for e in re.findall(EMOJI_REGEX, content):
        if e not in added and e in EMOJIS_BY_NAME:
            tags.append(EMOJIS_BY_NAME[e])
            added.add(e)

    return tags
