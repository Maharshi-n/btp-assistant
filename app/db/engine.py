from __future__ import annotations

import logging

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL


def _sync_url(url: str) -> str:
    """Convert async DB URL to sync equivalent."""
    return (
        url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
           .replace("sqlite+aiosqlite://", "sqlite://")
    )


_sync_engine = create_engine(_sync_url(DATABASE_URL), pool_pre_ping=True)
SyncSessionLocal = sessionmaker(_sync_engine, expire_on_commit=False)

# asyncpg on Windows (Python 3.14 + SelectorEventLoop) occasionally logs
# errors during connection pool teardown — these are cosmetic, the pool
# self-heals via pool_pre_ping. Silence them to avoid alarming the user.
logging.getLogger("sqlalchemy.pool.impl.AsyncAdaptedQueuePool").setLevel(logging.CRITICAL)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,  
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
