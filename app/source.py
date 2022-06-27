import re

from markdown import markdown
from sqlalchemy.orm import Session

from app import models
from app import webfinger
from app.actor import Actor
from app.actor import fetch_actor
from app.config import BASE_URL
from app.utils import emoji


def _set_a_attrs(attrs, new=False):
    attrs[(None, "target")] = "_blank"
    attrs[(None, "class")] = "external"
    attrs[(None, "rel")] = "noopener"
    attrs[(None, "title")] = attrs[(None, "href")]
    return attrs


_HASHTAG_REGEX = re.compile(r"(#[\d\w]+)")
_MENTION_REGEX = re.compile(r"@[\d\w_.+-]+@[\d\w-]+\.[\d\w\-.]+")


def _hashtagify(db: Session, content: str) -> tuple[str, list[dict[str, str]]]:
    tags = []
    hashtags = re.findall(_HASHTAG_REGEX, content)
    hashtags = sorted(set(hashtags), reverse=True)  # unique tags, longest first
    for hashtag in hashtags:
        tag = hashtag[1:]
        link = f'<a href="{BASE_URL}/t/{tag}" class="mention hashtag" rel="tag">#<span>{tag}</span></a>'  # noqa: E501
        tags.append(dict(href=f"{BASE_URL}/t/{tag}", name=hashtag, type="Hashtag"))
        content = content.replace(hashtag, link)
    return content, tags


def _mentionify(
    db: Session,
    content: str,
) -> tuple[str, list[dict[str, str]], list[Actor]]:
    tags = []
    mentioned_actors = []
    for mention in re.findall(_MENTION_REGEX, content):
        _, username, domain = mention.split("@")
        actor = (
            db.query(models.Actor).filter(models.Actor.handle == mention).one_or_none()
        )
        if not actor:
            actor_url = webfinger.get_actor_url(mention)
            if not actor_url:
                # FIXME(ts): raise an error?
                continue
            actor = fetch_actor(db, actor_url)

        mentioned_actors.append(actor)
        tags.append(dict(type="Mention", href=actor.url, name=mention))

        link = f'<span class="h-card"><a href="{actor.url}" class="u-url mention">@{username}</a></span>'  # noqa: E501
        content = content.replace(mention, link)
    return content, tags, mentioned_actors


def markdownify(
    db: Session,
    content: str,
    mentionify: bool = True,
    hashtagify: bool = True,
) -> tuple[str, list[dict[str, str]], list[Actor]]:
    """
    >>> content, tags = markdownify("Hello")

    """
    tags = []
    mentioned_actors: list[Actor] = []
    if hashtagify:
        content, hashtag_tags = _hashtagify(db, content)
        tags.extend(hashtag_tags)
    if mentionify:
        content, mention_tags, mentioned_actors = _mentionify(db, content)
        tags.extend(mention_tags)

    # Handle custom emoji
    tags.extend(emoji.tags(content))

    content = markdown(content, extensions=["mdx_linkify"])

    return content, tags, mentioned_actors
