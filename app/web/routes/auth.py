from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SECRET_KEY, SESSION_TTL_SECONDS
from app.db.engine import get_db
from app.db.models import Session, User

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")

_signer = URLSafeTimedSerializer(SECRET_KEY, salt="session")


def _sign(token: str) -> str:
    return _signer.dumps(token)


def _unsign(signed_token: str) -> str | None:
    """Return the raw token or None if signature/expiry is invalid."""
    try:
        return _signer.loads(signed_token, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()

    if user is None or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password."},
            status_code=401,
        )

    # Raw random token stored in DB; signed version goes into the cookie.
    raw_token = secrets.token_urlsafe(32)
    signed_token = _sign(raw_token)

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    expires_at = now + timedelta(seconds=SESSION_TTL_SECONDS)

    db_session = Session(token=raw_token, user_id=user.id, expires_at=expires_at)
    db.add(db_session)
    await db.commit()

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="session_token",
        value=signed_token,
        httponly=True,
        max_age=SESSION_TTL_SECONDS,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key="session_token")
    return response
