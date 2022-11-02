from pathlib import Path
from typing import Any
from typing import Callable

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from starlette.responses import JSONResponse

_DATA_DIR = Path().parent.resolve() / "data"
_Handler = Callable[..., Any]


class HTMLPage:
    def __init__(
        self,
        title: str,
        html_file: str,
        show_in_navbar: bool,
    ) -> None:
        self.title = title
        self.html_file = _DATA_DIR / html_file
        self.show_in_navbar = show_in_navbar


class RawHandler:
    def __init__(
        self,
        title: str,
        handler: Any,
        show_in_navbar: bool,
    ) -> None:
        self.title = title
        self.handler = handler
        self.show_in_navbar = show_in_navbar


_CUSTOM_ROUTES: dict[str, HTMLPage | RawHandler] = {}


def register_html_page(
    path: str,
    *,
    title: str,
    html_file: str,
    show_in_navbar: bool = True,
) -> None:
    if path in _CUSTOM_ROUTES:
        raise ValueError(f"{path} is already registered")

    _CUSTOM_ROUTES[path] = HTMLPage(title, html_file, show_in_navbar)


def register_raw_handler(
    path: str,
    *,
    title: str,
    handler: _Handler,
    show_in_navbar: bool = True,
) -> None:
    if path in _CUSTOM_ROUTES:
        raise ValueError(f"{path} is already registered")

    _CUSTOM_ROUTES[path] = RawHandler(title, handler, show_in_navbar)


class ActivityPubResponse(JSONResponse):
    media_type = "application/activity+json"


def _custom_page_handler(path: str, html_page: HTMLPage) -> Any:
    from app import templates
    from app.actor import LOCAL_ACTOR
    from app.config import is_activitypub_requested
    from app.database import AsyncSession
    from app.database import get_db_session

    async def _handler(
        request: Request,
        db_session: AsyncSession = Depends(get_db_session),
    ) -> templates.TemplateResponse | ActivityPubResponse:
        if path == "/" and is_activitypub_requested(request):
            return ActivityPubResponse(LOCAL_ACTOR.ap_actor)

        return await templates.render_template(
            db_session,
            request,
            "custom_page.html",
            {
                "page_content": html_page.html_file.read_text(),
                "title": html_page.title,
            },
        )

    return _handler


def get_custom_router() -> APIRouter | None:
    if not _CUSTOM_ROUTES:
        return None

    router = APIRouter()

    for path, handler in _CUSTOM_ROUTES.items():
        if isinstance(handler, HTMLPage):
            router.add_api_route(
                path, _custom_page_handler(path, handler), methods=["GET"]
            )
        else:
            router.add_api_route(path, handler.handler)

    return router
