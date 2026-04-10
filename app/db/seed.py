from __future__ import annotations

import bcrypt
from sqlalchemy import select

from app.config import ADMIN_PASSWORD, ADMIN_USERNAME
from app.db.engine import AsyncSessionLocal
from app.db.models import User


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
            return  # already seeded

        password_hash = bcrypt.hashpw(
            ADMIN_PASSWORD.encode(), bcrypt.gensalt()
        ).decode()
        user = User(username=ADMIN_USERNAME, password_hash=password_hash)
        session.add(user)
        await session.commit()
