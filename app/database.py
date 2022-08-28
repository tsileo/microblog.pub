from typing import Any
from typing import AsyncGenerator

from sqlalchemy import MetaData
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import DB_PATH
from app.config import DEBUG
from app.config import SQLALCHEMY_DATABASE_URL

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 15}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
async_engine = create_async_engine(
    DATABASE_URL, future=True, echo=DEBUG, connect_args={"timeout": 15}
)
async_session = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

Base: Any = declarative_base()
metadata_obj = MetaData()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
