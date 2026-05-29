"""Shared pytest fixtures: an isolated in-memory async DB session per test."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.engine import Base


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """Redirect WORKSPACE_DIR to a per-test temp dir so tests that write agent
    memory/episode files never pollute the real configured workspace (e.g. D:\\)."""
    import app.config as app_config
    monkeypatch.setattr(app_config, "WORKSPACE_DIR", tmp_path)


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    """A fresh in-memory SQLite DB with all tables created, torn down per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()
