"""Engine and session factory."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from hub.models import Base

SQLITE_DRIVER_PREFIX = "sqlite"
POOL_SIZE = 5
MAX_OVERFLOW = 5


def create_engine(url: str) -> AsyncEngine:
    parsed = make_url(url)
    if parsed.drivername.startswith(SQLITE_DRIVER_PREFIX):
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        return create_async_engine(url, connect_args={"check_same_thread": False})
    return create_async_engine(url, pool_pre_ping=True, pool_size=POOL_SIZE, max_overflow=MAX_OVERFLOW)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
