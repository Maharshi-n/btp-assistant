from __future__ import annotations

import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL


def _sync_url(url: str) -> str:
    """Convert async DB URL to sync equivalent."""
    return (
        url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
           .replace("sqlite+aiosqlite://", "sqlite://")
    )


_is_sqlite = "sqlite" in DATABASE_URL

# For sync engine: set WAL + busy_timeout on every new connection via event
_sync_engine = create_engine(
    _sync_url(DATABASE_URL),
    pool_pre_ping=True,
    connect_args={"timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:
    @event.listens_for(_sync_engine, "connect")
    def _set_sync_sqlite_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SyncSessionLocal = sessionmaker(_sync_engine, expire_on_commit=False)

logging.getLogger("sqlalchemy.pool.impl.AsyncAdaptedQueuePool").setLevel(logging.CRITICAL)


class Base(DeclarativeBase):
    pass


# For async engine: use NullPool so every session gets its own connection,
# then set pragmas via creator function passed through connect_args.
# aiosqlite wraps the underlying sqlite3 connection — we set pragmas in init_db
# and rely on WAL being a database-level persistent setting (survives reconnects).
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={"timeout": 30} if _is_sqlite else {},
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables on startup. Also sets SQLite pragmas on the connection."""
    async with engine.begin() as conn:
        if _is_sqlite:
            # WAL mode persists at the database file level — setting it once
            # is enough. busy_timeout must be set per-connection via the raw
            # aiosqlite connection since SQLAlchemy doesn't expose it otherwise.
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(Base.metadata.create_all)
        # Migrations: add columns that may not exist in older DBs
        if _is_sqlite:
            try:
                await conn.execute(text("ALTER TABLE automations ADD COLUMN raw_description TEXT"))
            except Exception:
                pass
        else:
            try:
                await conn.execute(text("ALTER TABLE automations ADD COLUMN IF NOT EXISTS raw_description TEXT"))
            except Exception:
                pass  # column already exists

    # Set busy_timeout on every future async connection via pool checkout event.
    # WAL mode is already persistent in the file, but busy_timeout is per-connection.
    if _is_sqlite:
        @event.listens_for(engine.sync_engine, "connect")
        def _set_async_sqlite_pragmas(dbapi_conn, _rec):
            # dbapi_conn here is the raw aiosqlite connection wrapper.
            # We can't await here, so we use the synchronous sqlite3 cursor
            # that aiosqlite exposes on ._connection when available.
            try:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
            except Exception:
                pass
