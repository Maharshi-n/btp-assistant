from __future__ import annotations

import os
from pathlib import Path

import bcrypt
from sqlalchemy import select

from app.config import ADMIN_PASSWORD, ADMIN_USERNAME, WORKSPACE_DIR
from app.db.engine import AsyncSessionLocal
from app.db.models import User, WorkspaceLocation


async def seed_admin() -> None:
    """Insert the admin user on first startup if no user exists yet."""
    if not ADMIN_PASSWORD:
        raise RuntimeError(
            "ADMIN_PASSWORD is not set in .env. "
            "Please add it before starting the server."
        )

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User))
        existing = result.scalars().first()
        if existing is not None:
            return

        password_hash = bcrypt.hashpw(
            ADMIN_PASSWORD.encode(), bcrypt.gensalt()
        ).decode()
        user = User(username=ADMIN_USERNAME, password_hash=password_hash)
        session.add(user)
        await session.commit()


async def seed_primary_workspace() -> None:
    """Ensure the primary WorkspaceLocation row exists for WORKSPACE_DIR."""
    resolved = str(Path(os.path.realpath(str(WORKSPACE_DIR))))
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(WorkspaceLocation).where(WorkspaceLocation.is_primary == True)  # noqa: E712
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return
        session.add(WorkspaceLocation(
            path=resolved,
            label="Main workspace",
            is_primary=True,
            writable=True,
        ))
        await session.commit()
