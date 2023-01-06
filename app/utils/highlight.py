import base64
import hashlib
from functools import lru_cache

from bs4 import BeautifulSoup  # type: ignore
from pygments import highlight as phighlight  # type: ignore
from pygments.formatters import HtmlFormatter  # type: ignore
from pygments.lexers import get_lexer_by_name  # type: ignore
from pygments.lexers import guess_lexer  # type: ignore

from app.config import CODE_HIGHLIGHTING_THEME

_FORMATTER = HtmlFormatter(style=CODE_HIGHLIGHTING_THEME)

HIGHLIGHT_CSS = _FORMATTER.get_style_defs()
HIGHLIGHT_CSS_HASH = base64.b64encode(
    hashlib.sha256(HIGHLIGHT_CSS.encode()).digest()
).decode()


@lru_cache(256)
def highlight(html: str) -> str:
    soup = BeautifulSoup(html, "html5lib")
    for code in soup.find_all("code"):
        if not code.parent.name == "pre":
            continue

        # Replace <br> tags with line breaks (Mastodon sends code like this)
        code_content = (
            code.encode_contents().decode().replace("<br>", "\n").replace("<br/>", "\n")
        )

        # If this comes from a microblog.pub instance we may have the language
        # in the class name
        if "data-microblogpub-lexer" in code.attrs:
            try:
                lexer = get_lexer_by_name(code.attrs["data-microblogpub-lexer"])
            except Exception:
                lexer = guess_lexer(code_content)

            # Replace the code with Pygment output
            # XXX: the HTML escaping causes issue with Python type annotations
            code_content = code_content.replace(") -&gt; ", ") -> ")
            code.parent.replaceWith(
                BeautifulSoup(
                    phighlight(code_content, lexer, _FORMATTER), "html5lib"
                ).body.next
            )
        else:
            code.name = "div"
            code["class"] = code.get("class", []) + ["highlight"]

    return soup.body.encode_contents().decode()
