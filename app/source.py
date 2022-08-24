import re
import typing

from markdown import markdown
from sqlalchemy import select

from app import webfinger
from app.config import BASE_URL
from app.database import AsyncSession
from app.utils import emoji

if typing.TYPE_CHECKING:
    from app.actor import Actor


def _set_a_attrs(attrs, new=False):
    attrs[(None, "target")] = "_blank"
    attrs[(None, "class")] = "external"
    attrs[(None, "rel")] = "noopener"
    attrs[(None, "title")] = attrs[(None, "href")]
    return attrs


_HASHTAG_REGEX = re.compile(r"(#[\d\w]+)")
_MENTION_REGEX = re.compile(r"@[\d\w_.+-]+@[\d\w-]+\.[\d\w\-.]+")


def hashtagify(content: str) -> tuple[str, list[dict[str, str]]]:
    tags = []
    hashtags = re.findall(_HASHTAG_REGEX, content)
    hashtags = sorted(set(hashtags), reverse=True)  # unique tags, longest first
    for hashtag in hashtags:
        tag = hashtag[1:]
        link = f'<a href="{BASE_URL}/t/{tag}" class="mention hashtag" rel="tag">#<span>{tag}</span></a>'  # noqa: E501
        tags.append(dict(href=f"{BASE_URL}/t/{tag}", name=hashtag, type="Hashtag"))
        content = content.replace(hashtag, link)
    return content, tags


async def _mentionify(
    db_session: AsyncSession,
    content: str,
) -> tuple[str, list[dict[str, str]], list["Actor"]]:
    from app import models
    from app.actor import fetch_actor

    tags = []
    mentioned_actors = []
    for mention in re.findall(_MENTION_REGEX, content):
        _, username, domain = mention.split("@")
        actor = (
            await db_session.execute(
                select(models.Actor).where(models.Actor.handle == mention)
            )
        ).scalar_one_or_none()
        if not actor:
            actor_url = await webfinger.get_actor_url(mention)
            if not actor_url:
                # FIXME(ts): raise an error?
                continue
            actor = await fetch_actor(db_session, actor_url)

        mentioned_actors.append(actor)
        tags.append(dict(type="Mention", href=actor.ap_id, name=mention))

        link = f'<span class="h-card"><a href="{actor.url}" class="u-url mention">{actor.handle}</a></span>'  # noqa: E501
        content = content.replace(mention, link)
    return content, tags, mentioned_actors


async def markdownify(
    db_session: AsyncSession,
    content: str,
    enable_mentionify: bool = True,
    enable_hashtagify: bool = True,
) -> tuple[str, list[dict[str, str]], list["Actor"]]:
    """
    >>> content, tags = markdownify("Hello")

    """
    tags = []
    mentioned_actors: list["Actor"] = []
    if enable_hashtagify:
        content, hashtag_tags = hashtagify(content)
        tags.extend(hashtag_tags)
    if enable_mentionify:
        content, mention_tags, mentioned_actors = await _mentionify(db_session, content)
        tags.extend(mention_tags)

    # Handle custom emoji
    tags.extend(emoji.tags(content))

    content = markdown(content, extensions=["mdx_linkify", "fenced_code"])

    return content, tags, mentioned_actors
