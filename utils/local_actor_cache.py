from pathlib import Path

_CACHE_FILE = Path(__file__).parent.absolute() / ".." / "config" / "local_actor_hash"


def is_actor_updated(actor_hash: str) -> bool:
    actor_updated = False
    cache_exists = _CACHE_FILE.exists()
    if cache_exists:
        current_hash = _CACHE_FILE.read_text()
        if actor_hash != current_hash:
            actor_updated = True

    if actor_updated or not cache_exists:
        with _CACHE_FILE.open("w") as f:
            f.write(actor_hash)

    return actor_updated
