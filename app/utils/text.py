import re
import unicodedata


def slugify(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-_")
