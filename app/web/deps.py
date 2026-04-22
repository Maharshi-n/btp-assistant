from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Cookie, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import Session, User


class NotAuthenticated(Exception):
    """Raised by require_user when no valid session cookie is present."""


async def require_user(
    request: Request,
    session_token: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Return the authenticated User or raise NotAuthenticated."""
    if not session_token:
        raise NotAuthenticated()

    # Unsign the cookie value to get the raw DB token.
    # Import here to avoid circular imports.
    from app.web.routes.auth import _unsign

    raw_token = _unsign(session_token)
    if raw_token is None:
        raise NotAuthenticated()

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    # Retry once on asyncpg "different loop" pool errors — the pool self-heals
    # on the second attempt after discarding the stale connection.
    for _attempt in range(2):
        try:
            result = await db.execute(
                select(Session).where(
                    Session.token == raw_token,
                    Session.expires_at > now,
                )
            )
            db_session = result.scalars().first()
            if db_session is None:
                raise NotAuthenticated()

            user_result = await db.execute(select(User).where(User.id == db_session.user_id))
            user = user_result.scalars().first()
            if user is None:
                raise NotAuthenticated()

            return user
        except NotAuthenticated:
            raise
        except Exception:
            if _attempt == 1:
                raise
            # Stale asyncpg connection — open a fresh session and retry
            await db.close()
            from app.db.engine import AsyncSessionLocal
            db = AsyncSessionLocal()

    raise NotAuthenticated()  # unreachable, satisfies type checker
