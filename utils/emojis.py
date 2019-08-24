import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Set

from little_boxes import activitypub as ap

EMOJI_REGEX = re.compile(r"(:[\d\w]+:)")

EMOJIS: Dict[str, Dict[str, Any]] = {}
EMOJIS_BY_NAME: Dict[str, Dict[str, Any]] = {}


def _load_emojis(root_dir: Path, base_url: str) -> None:
    if EMOJIS:
        return
    for emoji in (root_dir / "static" / "emojis").iterdir():
        mt = mimetypes.guess_type(emoji.name)[0]
        if mt and mt.startswith("image/"):
            name = emoji.name.split(".")[0]
            ap_emoji = dict(
                type=ap.ActivityType.EMOJI.value,
                name=f":{name}:",
                updated=ap.format_datetime(datetime.fromtimestamp(0.0).astimezone()),
                id=f"{base_url}/emoji/{name}",
                icon={
                    "mediaType": mt,
                    "type": ap.ActivityType.IMAGE.value,
                    "url": f"{base_url}/static/emojis/{emoji.name}",
                },
            )
            EMOJIS[emoji.name] = ap_emoji
            EMOJIS_BY_NAME[ap_emoji["name"]] = ap_emoji


def tags(content: str) -> List[Dict[str, Any]]:
    tags: List[Dict[str, Any]] = []
    added: Set[str] = set()
    for e in re.findall(EMOJI_REGEX, content):
        if e not in added and e in EMOJIS_BY_NAME:
            tags.append(EMOJIS_BY_NAME[e])
            added.add(e)

    return tags
