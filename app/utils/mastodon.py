from pathlib import Path

from loguru import logger

from app.webfinger import get_actor_url


def _load_mastodon_following_accounts_csv_file(path: str) -> list[str]:
    handles = []
    for line in Path(path).read_text().splitlines()[1:]:
        handle = line.split(",")[0]
        handles.append(handle)

    return handles


async def get_actor_urls_from_following_accounts_csv_file(
    path: str,
) -> list[tuple[str, str]]:
    actor_urls = []
    for handle in _load_mastodon_following_accounts_csv_file(path):
        try:
            actor_url = await get_actor_url(handle)
        except Exception:
            logger.error("Failed to fetch actor URL for {handle=}")
        else:
            if actor_url:
                actor_urls.append((handle, actor_url))
            else:
                logger.info(f"No actor URL found for {handle=}")

    return actor_urls
