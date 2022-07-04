from pathlib import Path

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape
from markdown import markdown


def markdownify(content: str) -> str:
    return markdown(content, extensions=["mdx_linkify"])


def main() -> None:
    # Setup Jinja
    loader = FileSystemLoader("docs/templates")
    env = Environment(loader=loader, autoescape=select_autoescape())
    template = env.get_template("layout.html")

    Path("docs/dist").mkdir(exist_ok=True)

    readme = Path("README.md")
    template.stream(
        content=markdownify(readme.read_text().removeprefix("# microblog.pub"))
    ).dump("docs/dist/index.html")


if __name__ == "__main__":
    main()
