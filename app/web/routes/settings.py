"""Phase 8: Settings page with Google OAuth connect/disconnect, change password, clear chats."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import Message, Thread, User
from app.web.deps import require_user

BASE_DIR = Path(__file__).resolve().parent.parent.parent

router = APIRouter(prefix="/settings")
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

# Google OAuth scopes
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

_REDIRECT_URI = "http://localhost:8000/settings/google/callback"


def _resolve_db_path() -> str:
    import app.config as app_config
    url = app_config.DATABASE_URL
    if ":///" in url:
        return url.split("///", 1)[-1]
    return "app.db"


def _is_google_connected() -> bool:
    """Return True if a Google OAuth token row exists in the DB."""
    try:
        db_path = _resolve_db_path()
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM oauth_tokens WHERE provider = 'google' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:
        return False


@router.get("")
async def settings_page(
    request: Request,
    _user: User = Depends(require_user),
):
    import app.config as app_config
    from app.web.routes.chat import AVAILABLE_MODELS

    google_connected = _is_google_connected()
    google_configured = bool(app_config.GOOGLE_CLIENT_ID and app_config.GOOGLE_CLIENT_SECRET)
    fernet_configured = bool(app_config.FERNET_KEY)

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "google_connected": google_connected,
            "google_configured": google_configured,
            "fernet_configured": fernet_configured,
            "workspace_dir": str(app_config.WORKSPACE_DIR),
            "available_models": list(AVAILABLE_MODELS),
            "telegram_bot_token": app_config.TELEGRAM_BOT_TOKEN,
            "telegram_chat_id": app_config.TELEGRAM_CHAT_ID,
        },
    )


@router.post("/workspace")
async def update_workspace(
    workspace: str = Form(...),
    _user: User = Depends(require_user),
):
    """Change the workspace directory. Updates .env and applies immediately."""
    import app.config as app_config

    new_path = Path(workspace.strip()).resolve()

    if not new_path.exists():
        try:
            new_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return RedirectResponse(url=f"/settings?ws_error=create", status_code=302)

    if not new_path.is_dir():
        return RedirectResponse(url="/settings?ws_error=not_dir", status_code=302)

    # Update the live config value immediately
    app_config.WORKSPACE_DIR = new_path

    # Persist to .env so it survives server restarts
    env_path = BASE_DIR.parent / ".env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        found = False
        new_lines = []
        for line in lines:
            if line.startswith("WORKSPACE_DIR="):
                new_lines.append(f"WORKSPACE_DIR={new_path}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"WORKSPACE_DIR={new_path}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return RedirectResponse(url="/settings?ws_ok=1", status_code=302)


@router.post("/models")
async def update_models(
    request: Request,
    _user: User = Depends(require_user),
):
    """Update the list of available models from the settings UI."""
    from app.web.routes.chat import AVAILABLE_MODELS, _save_models

    form = await request.form()
    raw = form.get("models", "")
    # Accept newline-separated or comma-separated model IDs
    models = [m.strip() for m in raw.replace(",", "\n").splitlines() if m.strip()]
    if not models:
        return RedirectResponse(url="/settings?model_error=empty", status_code=302)

    # Update in-place so existing references (index page) stay valid
    AVAILABLE_MODELS.clear()
    AVAILABLE_MODELS.extend(models)
    _save_models(models)

    return RedirectResponse(url="/settings?model_ok=1", status_code=302)


@router.post("/telegram")
async def update_telegram(
    request: Request,
    _user: User = Depends(require_user),
):
    """Save Telegram bot token and chat ID. Updates live config + persists to .env."""
    import app.config as app_config

    form = await request.form()
    bot_token = form.get("bot_token", "").strip()
    chat_id = form.get("chat_id", "").strip()

    # Update live config immediately
    app_config.TELEGRAM_BOT_TOKEN = bot_token
    app_config.TELEGRAM_CHAT_ID = chat_id

    # Persist to .env
    env_path = BASE_DIR.parent / ".env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updates = {
            "TELEGRAM_BOT_TOKEN": bot_token,
            "TELEGRAM_CHAT_ID": chat_id,
        }
        new_lines = []
        found = set()
        for line in lines:
            matched = False
            for key in updates:
                if line.startswith(f"{key}="):
                    new_lines.append(f"{key}={updates[key]}")
                    found.add(key)
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        # Append any keys not already in file
        for key, val in updates.items():
            if key not in found:
                new_lines.append(f"{key}={val}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return RedirectResponse(url="/settings?tg_ok=1", status_code=302)


@router.post("/telegram/test")
async def test_telegram(
    _user: User = Depends(require_user),
):
    """Send a test Telegram message using current config. Returns JSON."""
    from app.tools.telegram_tools import telegram_send

    result = await telegram_send.ainvoke({"message": "RAION test notification — Telegram is connected!"})
    if result == "Sent.":
        return {"ok": True, "message": "Test message sent successfully."}
    else:
        return {"ok": False, "message": result}


@router.post("/change-password")
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the admin password."""
    if not bcrypt.checkpw(current_password.encode(), user.password_hash.encode()):
        return RedirectResponse(url="/settings?pw_error=wrong_current", status_code=302)

    if new_password != confirm_password:
        return RedirectResponse(url="/settings?pw_error=mismatch", status_code=302)

    if len(new_password) < 8:
        return RedirectResponse(url="/settings?pw_error=too_short", status_code=302)

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    user.password_hash = new_hash
    await db.commit()

    return RedirectResponse(url="/settings?pw_ok=1", status_code=302)


@router.post("/clear-chats")
async def clear_chats(
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete all messages and threads."""
    await db.execute(delete(Message))
    await db.execute(delete(Thread))
    await db.commit()
    return RedirectResponse(url="/settings?cleared=1", status_code=302)


@router.get("/google/connect")
async def google_connect(
    request: Request,
    _user: User = Depends(require_user),
):
    """Start the Google OAuth flow."""
    import app.config as app_config

    if not app_config.GOOGLE_CLIENT_ID or not app_config.GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=400,
            detail="GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env",
        )
    if not app_config.FERNET_KEY:
        raise HTTPException(
            status_code=400,
            detail="FERNET_KEY must be set in .env to encrypt tokens",
        )

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "installed": {
                "client_id": app_config.GOOGLE_CLIENT_ID,
                "client_secret": app_config.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [_REDIRECT_URI],
            }
        },
        scopes=_SCOPES,
        redirect_uri=_REDIRECT_URI,
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    # Stash state in a server-side session cookie equivalent — we just put it
    # in a response cookie for simplicity (it's a short-lived CSRF token).
    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie("google_oauth_state", state, max_age=600, httponly=True, samesite="lax")
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    _user: User = Depends(require_user),
):
    """Handle the OAuth callback, exchange code for tokens, store encrypted."""
    if error:
        return RedirectResponse(url="/settings?error=google_denied", status_code=302)

    import app.config as app_config
    from cryptography.fernet import Fernet
    from google_auth_oauthlib.flow import Flow

    stored_state = request.cookies.get("google_oauth_state", "")
    if state != stored_state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch. Please try again.")

    flow = Flow.from_client_config(
        {
            "installed": {
                "client_id": app_config.GOOGLE_CLIENT_ID,
                "client_secret": app_config.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [_REDIRECT_URI],
            }
        },
        scopes=_SCOPES,
        redirect_uri=_REDIRECT_URI,
        state=state,
    )

    # Fetch token (allow http for localhost dev)
    import os
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow.fetch_token(code=code)

    creds = flow.credentials
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or _SCOPES),
    }

    fernet = Fernet(app_config.FERNET_KEY.encode())
    encrypted = fernet.encrypt(json.dumps(token_data).encode()).decode()

    now = datetime.now(timezone.utc).isoformat()
    db_path = _resolve_db_path()
    conn = sqlite3.connect(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM oauth_tokens WHERE provider = 'google' LIMIT 1"
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE oauth_tokens SET token_json = ?, refreshed_at = ? WHERE provider = 'google'",
                (encrypted, now),
            )
        else:
            conn.execute(
                "INSERT INTO oauth_tokens (provider, token_json, refreshed_at) VALUES ('google', ?, ?)",
                (encrypted, now),
            )
        conn.commit()
    finally:
        conn.close()

    response = RedirectResponse(url="/settings?connected=1", status_code=302)
    response.delete_cookie("google_oauth_state")
    return response


@router.post("/google/disconnect")
async def google_disconnect(
    _user: User = Depends(require_user),
):
    """Remove the stored Google OAuth token."""
    db_path = _resolve_db_path()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM oauth_tokens WHERE provider = 'google'")
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/settings?disconnected=1", status_code=302)
