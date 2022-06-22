from contextlib import contextmanager

import fastapi

from app import actor
from app import httpsig
from app.config import session_serializer
from app.main import app


@contextmanager
def mock_httpsig_checker(ra: actor.RemoteActor):
    async def httpsig_checker(
        request: fastapi.Request,
    ) -> httpsig.HTTPSigInfo:
        return httpsig.HTTPSigInfo(
            has_valid_signature=True,
            signed_by_ap_actor_id=ra.ap_id,
        )

    app.dependency_overrides[httpsig.httpsig_checker] = httpsig_checker
    try:
        yield
    finally:
        del app.dependency_overrides[httpsig.httpsig_checker]


def generate_admin_session_cookies() -> dict[str, str]:
    return {"session": session_serializer.dumps({"is_logged_in": True})}
