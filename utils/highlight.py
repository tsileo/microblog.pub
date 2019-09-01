from functools import lru_cache

from bs4 import BeautifulSoup
from pygments import highlight as phighlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import guess_lexer

from config import THEME_STYLE
from config import ThemeStyle

_FORMATTER = HtmlFormatter(
    style="default" if THEME_STYLE == ThemeStyle.LIGHT else "vim"
)

HIGHLIGHT_CSS = _FORMATTER.get_style_defs()


@lru_cache(512)
def highlight(html: str) -> str:
    soup = BeautifulSoup(html, "html5lib")
    for code in soup.find_all("code"):
        if not code.parent.name == "pre":
            continue
        lexer = guess_lexer(code.text)
        tag = BeautifulSoup(
            phighlight(code.text, lexer, _FORMATTER), "html5lib"
        ).body.next
        pre = code.parent
        pre.replaceWith(tag)
    out = soup.body
    out.name = "div"
    return str(out)
