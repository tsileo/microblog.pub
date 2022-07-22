import sass  # type: ignore
from PIL import Image
from PIL import ImageColor
from PIL import ImageDraw


def _get_primary_color() -> str:
    """Small hack to get the theme primary color."""
    compiled = sass.compile(
        string=(
            "@import 'app/scss/main.scss';\n"
            "#favicon-color { color: $primary-color; }"
        )
    )
    return compiled[len(compiled) - 11 : -4]


def build_favicon() -> None:
    """Builds a basic favicon with the theme primary color."""
    im = Image.new("RGB", (32, 32), ImageColor.getrgb(_get_primary_color()))
    ImageDraw.Draw(im)
    im.save("app/static/favicon.ico")
