import shutil
from pathlib import Path

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape
from markdown import markdown

from app.config import VERSION
from app.utils.datetime import now


def markdownify(content: str) -> str:
    return markdown(
        content, extensions=["mdx_linkify", "fenced_code", "codehilite", "toc"]
    )


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
    template.stream(
        content=markdownify(readme.read_text().removeprefix("# microblog.pub")),
        version=VERSION,
        path="/",
        last_updated=last_updated,
    ).dump("docs/dist/index.html")

    install = Path("docs/install.md")
    template.stream(
        content=markdownify(install.read_text()),
        version=VERSION,
        path="/installing.html",
        last_updated=last_updated,
    ).dump("docs/dist/installing.html")

    user_guide = Path("docs/user_guide.md")
    template.stream(
        content=markdownify(user_guide.read_text()),
        version=VERSION,
        path="/user_guide.html",
        last_updated=last_updated,
    ).dump("docs/dist/user_guide.html")

    developer_guide = Path("docs/developer_guide.md")
    template.stream(
        content=markdownify(developer_guide.read_text()),
        version=VERSION,
        path="/developer_guide.html",
        last_updated=last_updated,
    ).dump("docs/dist/developer_guide.html")


if __name__ == "__main__":
    main()
