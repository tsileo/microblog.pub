import re
import typing

from loguru import logger
from mistletoe import Document  # type: ignore
from mistletoe.block_token import CodeFence  # type: ignore
from mistletoe.html_renderer import HTMLRenderer  # type: ignore
from mistletoe.span_token import SpanToken  # type: ignore
from pygments.formatters import HtmlFormatter  # type: ignore
from pygments.lexers import get_lexer_by_name as get_lexer  # type: ignore
from pygments.util import ClassNotFound  # type: ignore
from sqlalchemy import select

from app import webfinger
from app.config import BASE_URL
from app.config import CODE_HIGHLIGHTING_THEME
from app.database import AsyncSession
from app.utils import emoji

if typing.TYPE_CHECKING:
    from app.actor import Actor

_FORMATTER = HtmlFormatter(style=CODE_HIGHLIGHTING_THEME)
_HASHTAG_REGEX = re.compile(r"(#[\d\w]+)")
_MENTION_REGEX = re.compile(r"(@[\d\w_.+-]+@[\d\w-]+\.[\d\w\-.]+)")
_URL_REGEX = re.compile(
    "(https?:\\/\\/(?:www\\.)?[-a-zA-Z0-9@:%._\\+~#=]{1,256}\\.[a-zA-Z0-9()]{1,6}\\b(?:[-a-zA-Z0-9()@:%_\\+.~#?&\\/=]*))"  # noqa: E501
)


class AutoLink(SpanToken):
    parse_inner = False
    precedence = 1
    pattern = _URL_REGEX

    def __init__(self, match_obj: re.Match) -> None:
        self.target = match_obj.group()


class Mention(SpanToken):
    parse_inner = False
    precedence = 10
    pattern = _MENTION_REGEX

    def __init__(self, match_obj: re.Match) -> None:
        self.target = match_obj.group()


class Hashtag(SpanToken):
    parse_inner = False
    precedence = 10
    pattern = _HASHTAG_REGEX

    def __init__(self, match_obj: re.Match) -> None:
        self.target = match_obj.group()


class CustomRenderer(HTMLRenderer):
    def __init__(
        self,
        mentioned_actors: dict[str, "Actor"] = {},
        enable_mentionify: bool = True,
        enable_hashtagify: bool = True,
    ) -> None:
        extra_tokens = []
        if enable_mentionify:
            extra_tokens.append(Mention)
        if enable_hashtagify:
            extra_tokens.append(Hashtag)
        super().__init__(AutoLink, *extra_tokens)

        self.tags: list[dict[str, str]] = []
        self.mentioned_actors = mentioned_actors

    def render_auto_link(self, token: AutoLink) -> str:
        template = '<a href="{target}" rel="noopener">{inner}</a>'
        target = self.escape_url(token.target)
        return template.format(target=target, inner=target)

    def render_mention(self, token: Mention) -> str:
        mention = token.target
        suffix = ""
        if mention.endswith("."):
            mention = mention[:-1]
            suffix = "."
        actor = self.mentioned_actors.get(mention)
        if not actor:
            return mention

        self.tags.append(dict(type="Mention", href=actor.ap_id, name=mention))

        link = f'<span class="h-card"><a href="{actor.url}" class="u-url mention">{actor.handle}</a></span>{suffix}'  # noqa: E501
        return link

    def render_hashtag(self, token: Hashtag) -> str:
        tag = token.target[1:]
        link = f'<a href="{BASE_URL}/t/{tag.lower()}" class="mention hashtag" rel="tag">#<span>{tag}</span></a>'  # noqa: E501
        self.tags.append(
            dict(
                href=f"{BASE_URL}/t/{tag.lower()}",
                name=token.target.lower(),
                type="Hashtag",
            )
        )
        return link

    def render_block_code(self, token: CodeFence) -> str:
        lexer_attr = ""
        try:
            lexer = get_lexer(token.language)
            lexer_attr = f' data-microblogpub-lexer="{lexer.aliases[0]}"'
        except ClassNotFound:
            pass

        code = token.children[0].content
        return f"<pre><code{lexer_attr}>\n{code}\n</code></pre>"


async def _prefetch_mentioned_actors(
    db_session: AsyncSession,
    content: str,
) -> dict[str, "Actor"]:
    from app import models
    from app.actor import fetch_actor

    actors = {}

    for mention in re.findall(_MENTION_REGEX, content):
        if mention in actors:
            continue

        # XXX: the regex catches stuff like `@toto@example.com.`
        if mention.endswith("."):
            mention = mention[:-1]

        try:
            _, username, domain = mention.split("@")
            actor = (
                await db_session.execute(
                    select(models.Actor).where(
                        models.Actor.handle == mention,
                        models.Actor.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if not actor:
                actor_url = await webfinger.get_actor_url(mention)
                if not actor_url:
                    # FIXME(ts): raise an error?
                    continue
                actor = await fetch_actor(db_session, actor_url)

            actors[mention] = actor
        except Exception:
            logger.exception(f"Failed to prefetch {mention}")

    return actors


def hashtagify(
    content: str,
) -> tuple[str, list[dict[str, str]]]:
    tags = []
    with CustomRenderer(
        mentioned_actors={},
        enable_mentionify=False,
        enable_hashtagify=True,
    ) as renderer:
        rendered_content = renderer.render(Document(content))
        tags.extend(renderer.tags)

    # Handle custom emoji
    tags.extend(emoji.tags(content))

    return rendered_content, tags


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
    mentioned_actors: dict[str, "Actor"] = {}
    if enable_mentionify:
        mentioned_actors = await _prefetch_mentioned_actors(db_session, content)

    with CustomRenderer(
        mentioned_actors=mentioned_actors,
        enable_mentionify=enable_mentionify,
        enable_hashtagify=enable_hashtagify,
    ) as renderer:
        rendered_content = renderer.render(Document(content))
        tags.extend(renderer.tags)

    # Handle custom emoji
    tags.extend(emoji.tags(content))

    return rendered_content, dedup_tags(tags), list(mentioned_actors.values())


def dedup_tags(tags: list[dict[str, str]]) -> list[dict[str, str]]:
    idx = set()
    deduped_tags = []
    for tag in tags:
        tag_idx = (tag["type"], tag["name"])
        if tag_idx in idx:
            continue

        idx.add(tag_idx)
        deduped_tags.append(tag)

    return deduped_tags
