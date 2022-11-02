from typing import Any
from typing import Awaitable
from typing import Callable

from fastapi import Depends
from fastapi import Request
from fastapi.responses import JSONResponse

from app.actor import LOCAL_ACTOR
from app.config import is_activitypub_requested
from app.database import AsyncSession
from app.database import get_db_session

_Handler = Callable[[Request, AsyncSession], Awaitable[Any]]


def build_custom_index_handler(handler: _Handler) -> _Handler:
    async def custom_index(
        request: Request,
        db_session: AsyncSession = Depends(get_db_session),
    ) -> Any:
        # Serve the AP actor if requested
        if is_activitypub_requested(request):
            return JSONResponse(
                LOCAL_ACTOR.ap_actor,
                media_type="application/activity+json",
            )

        # Defer to the custom handler
        return await handler(request, db_session)

    return custom_index
