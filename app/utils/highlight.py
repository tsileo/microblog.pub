from functools import lru_cache

from bs4 import BeautifulSoup  # type: ignore
from pygments import highlight as phighlight  # type: ignore
from pygments.formatters import HtmlFormatter  # type: ignore
from pygments.lexers import guess_lexer  # type: ignore

from app.config import CODE_HIGHLIGHTING_THEME

_FORMATTER = HtmlFormatter(style=CODE_HIGHLIGHTING_THEME)

HIGHLIGHT_CSS = _FORMATTER.get_style_defs()


@lru_cache(256)
def highlight(html: str) -> str:
    soup = BeautifulSoup(html, "html5lib")
    for code in soup.find_all("code"):
        if not code.parent.name == "pre":
            continue
        code_content = (
            code.encode_contents().decode().replace("<br>", "\n").replace("<br/>", "\n")
        )
        lexer = guess_lexer(code_content)
        tag = BeautifulSoup(
            phighlight(code_content, lexer, _FORMATTER), "html5lib"
        ).body.next
        pre = code.parent
        pre.replaceWith(tag)
    out = soup.body
    out.name = "div"
    return str(out)
