import re
import shutil
import typing
from pathlib import Path
from typing import Any

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape
from mistletoe import Document  # type: ignore
from mistletoe import HTMLRenderer  # type: ignore
from mistletoe import block_token  # type: ignore
from pygments import highlight  # type: ignore
from pygments.formatters import HtmlFormatter  # type: ignore
from pygments.lexers import get_lexer_by_name as get_lexer  # type: ignore
from pygments.lexers import guess_lexer  # type: ignore

from app.config import VERSION
from app.source import CustomRenderer
from app.utils.datetime import now

_FORMATTER = HtmlFormatter()
_FORMATTER.noclasses = True


class DocRenderer(CustomRenderer):
    def __init__(
        self,
        depth=5,
        omit_title=True,
        filter_conds=[],
    ) -> None:
        super().__init__(
            enable_mentionify=False,
            enable_hashtagify=False,
        )
        self._headings: list[tuple[int, str, str]] = []
        self._ids: set[str] = set()
        self.depth = depth
        self.omit_title = omit_title
        self.filter_conds = filter_conds

    @property
    def toc(self):
        """
        Returns table of contents as a block_token.List instance.
        """

        def get_indent(level):
            if self.omit_title:
                level -= 1
            return " " * 4 * (level - 1)

        def build_list_item(heading):
            level, content, title_id = heading
            template = '{indent}- <a href="#{id}" rel="nofollow">{content}</a>\n'
            return template.format(
                indent=get_indent(level), content=content, id=title_id
            )

        lines = [build_list_item(heading) for heading in self._headings]
        items = block_token.tokenize(lines)
        return items[0]

    def render_heading(self, token):
        """
        Overrides super().render_heading; stores rendered heading first,
        then returns it.
        """
        template = '<h{level} id="{id}">{inner}</h{level}>'
        inner = self.render_inner(token)
        title_id = inner.lower().replace(" ", "-")
        if title_id in self._ids:
            i = 1
            while 1:
                title_id = f"{title_id}_{i}"
                if title_id not in self._ids:
                    break
        self._ids.add(title_id)
        rendered = template.format(level=token.level, inner=inner, id=title_id)
        content = self.parse_rendered_heading(rendered)

        if not (
            self.omit_title
            and token.level == 1
            or token.level > self.depth
            or any(cond(content) for cond in self.filter_conds)
        ):
            self._headings.append((token.level, content, title_id))
        return rendered

    @staticmethod
    def parse_rendered_heading(rendered):
        """
        Helper method; converts rendered heading to plain text.
        """
        return re.sub(r"<.+?>", "", rendered)

    def render_block_code(self, token: typing.Any) -> str:
        code = token.children[0].content
        lexer = get_lexer(token.language) if token.language else guess_lexer(code)
        return highlight(code, lexer, _FORMATTER)


def markdownify(content: str) -> tuple[str, Any]:
    with DocRenderer() as renderer:
        rendered_content = renderer.render(Document(content))

    with HTMLRenderer() as html_renderer:
        toc = html_renderer.render(renderer.toc)

    return rendered_content, toc


def main() -> None:
    # Setup Jinja
    loader = FileSystemLoader("docs/templates")
    env = Environment(loader=loader, autoescape=select_autoescape())
    template = env.get_template("layout.html")

    shutil.rmtree("docs/dist", ignore_errors=True)
    Path("docs/dist").mkdir(exist_ok=True)
    shutil.rmtree("docs/dist/static", ignore_errors=True)
    shutil.copytree("docs/static", "docs/dist/static")

    last_updated = now().replace(second=0, microsecond=0).isoformat()

    readme = Path("README.md")
    content, toc = markdownify(readme.read_text().removeprefix("# microblog.pub"))
    template.stream(
        content=content,
        version=VERSION,
        path="/",
        last_updated=last_updated,
    ).dump("docs/dist/index.html")

    install = Path("docs/install.md")
    content, toc = markdownify(install.read_text())
    template.stream(
        content=content.replace("[TOC]", toc),
        version=VERSION,
        path="/installing.html",
        last_updated=last_updated,
    ).dump("docs/dist/installing.html")

    user_guide = Path("docs/user_guide.md")
    content, toc = markdownify(user_guide.read_text())
    template.stream(
        content=content.replace("[TOC]", toc),
        version=VERSION,
        path="/user_guide.html",
        last_updated=last_updated,
    ).dump("docs/dist/user_guide.html")

    developer_guide = Path("docs/developer_guide.md")
    content, toc = markdownify(developer_guide.read_text())
    template.stream(
        content=content.replace("[TOC]", toc),
        version=VERSION,
        path="/developer_guide.html",
        last_updated=last_updated,
    ).dump("docs/dist/developer_guide.html")


if __name__ == "__main__":
    main()
