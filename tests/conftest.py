from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import orm

from app.database import Base
from app.database import engine
from app.database import get_db
from app.main import app
from tests.factories import _Session

# _Session = orm.sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _get_db_for_testing() -> Generator[orm.Session, None, None]:
    # try:
    yield _Session  # type: ignore
    # finally:
    #    session.close()


@pytest.fixture
def db() -> Generator:
    Base.metadata.create_all(bind=engine)
    # sess = orm.sessionmaker(bind=engine)()
    yield _Session
    # yield orm.scoped_session(orm.sessionmaker(bind=engine))
    try:
        Base.metadata.drop_all(bind=engine)
    except Exception:
        # XXX: for some reason, the teardown occasionally fails because of this
        pass


@pytest.fixture
def exclude_fastapi_middleware():
    """Workaround for https://github.com/encode/starlette/issues/472"""
    user_middleware = app.user_middleware.copy()
    app.user_middleware = []
    app.middleware_stack = app.build_middleware_stack()
    yield
    app.user_middleware = user_middleware
    app.middleware_stack = app.build_middleware_stack()


@pytest.fixture
def client(db, exclude_fastapi_middleware) -> Generator:
    app.dependency_overrides[get_db] = _get_db_for_testing
    with TestClient(app) as c:
        yield c
