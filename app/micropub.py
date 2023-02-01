from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from loguru import logger

from app import activitypub as ap
from app.boxes import get_outbox_object_by_ap_id
from app.boxes import send_create
from app.boxes import send_delete
from app.database import AsyncSession
from app.database import get_db_session
from app.indieauth import AccessTokenInfo
from app.indieauth import verify_access_token

router = APIRouter()


@router.get("/micropub")
async def micropub_endpoint(
    request: Request,
    access_token_info: AccessTokenInfo = Depends(verify_access_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any] | JSONResponse:
    if request.query_params.get("q") == "config":
        return {}

    elif request.query_params.get("q") == "source":
        url = request.query_params.get("url")
        outbox_object = await get_outbox_object_by_ap_id(db_session, url)
        if not outbox_object:
            return JSONResponse(
                content={
                    "error": "invalid_request",
                    "error_description": "No post with this URL",
                },
                status_code=400,
            )

        extra_props: dict[str, list[str]] = {}

        return {
            "type": ["h-entry"],
            "properties": {
                "published": [
                    outbox_object.ap_published_at.isoformat()  # type: ignore
                ],
                "content": [outbox_object.source],
                **extra_props,
            },
        }

    return {}


def _prop_get(dat: dict[str, Any], key: str) -> str:
    val = dat[key]
    if isinstance(val, list):
        return val[0]
    else:
        return val


@router.post("/micropub")
async def post_micropub_endpoint(
    request: Request,
    access_token_info: AccessTokenInfo = Depends(verify_access_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse | JSONResponse:
    form_data = await request.form()
    is_json = False
    if not form_data:
        form_data = await request.json()
        is_json = True

    insufficient_scope_resp = JSONResponse(
        status_code=401, content={"error": "insufficient_scope"}
    )

    if "action" in form_data:
        if form_data["action"] in ["delete", "update"]:
            outbox_object = await get_outbox_object_by_ap_id(
                db_session, form_data["url"]
            )
            if not outbox_object:
                return JSONResponse(
                    content={
                        "error": "invalid_request",
                        "error_description": "No post with this URL",
                    },
                    status_code=400,
                )

            if form_data["action"] == "delete":
                if "delete" not in access_token_info.scopes:
                    return insufficient_scope_resp
                logger.info(f"Deleting object {outbox_object.ap_id}")
                await send_delete(db_session, outbox_object.ap_id)  # type: ignore
                return JSONResponse(content={}, status_code=200)

            elif form_data["action"] == "update":
                if "update" not in access_token_info.scopes:
                    return insufficient_scope_resp

                # TODO(ts): support update
                # "replace": {"content": ["new content"]}

                logger.info(f"Updating object {outbox_object.ap_id}: {form_data}")
                return JSONResponse(content={}, status_code=200)
            else:
                raise ValueError("Should never happen")
        else:
            return JSONResponse(
                content={
                    "error": "invalid_request",
                    "error_description": f'Unsupported action: {form_data["action"]}',
                },
                status_code=400,
            )

    if "create" not in access_token_info.scopes:
        return insufficient_scope_resp

    if is_json:
        entry_type = _prop_get(form_data, "type")  # type: ignore
    else:
        h = "entry"
        if "h" in form_data:
            h = form_data["h"]
        entry_type = f"h-{h}"

    logger.info(f"Creating {entry_type=} with {access_token_info=}")

    if entry_type != "h-entry":
        return JSONResponse(
            content={
                "error": "invalid_request",
                "error_description": "Only h-entry are supported",
            },
            status_code=400,
        )

    # TODO(ts): support creating Article (with a name)

    if is_json:
        content = _prop_get(form_data["properties"], "content")  # type: ignore
    else:
        content = form_data["content"]

    public_id, _ = await send_create(
        db_session,
        "Note",
        content,
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    return JSONResponse(
        content={},
        status_code=201,
        headers={
            "Location": request.url_for("outbox_by_public_id", public_id=public_id)
        },
    )
