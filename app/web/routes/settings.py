"""Phase 8: Settings page with Google OAuth connect/disconnect, change password, clear chats."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import AsyncSessionLocal, get_db
from app.db.models import Message, OAuthToken, Thread, User, WorkspaceLocation
from app.web.deps import require_user

BASE_DIR = Path(__file__).resolve().parent.parent.parent

router = APIRouter(prefix="/settings")


def _update_env_var(key: str, value: str) -> None:
    """Update or append a single key=value line in the .env file."""
    env_path = BASE_DIR.parent / ".env"
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

# Google OAuth scopes
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

_REDIRECT_URI = "http://localhost:8000/settings/google/callback"


async def _is_google_connected() -> bool:
    """Return True if a Google OAuth token row exists in the DB."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(OAuthToken.id).where(OAuthToken.provider == "google").limit(1)
            )
            return result.scalar() is not None
    except Exception:
        return False


@router.get("")
async def settings_page(
    request: Request,
    _user: User = Depends(require_user),
):
    import app.config as app_config
    from app.web.routes.chat import AVAILABLE_MODELS

    google_connected = await _is_google_connected()
    google_configured = bool(app_config.GOOGLE_CLIENT_ID and app_config.GOOGLE_CLIENT_SECRET)
    fernet_configured = bool(app_config.FERNET_KEY)

    # Load secondary workspace locations for the template
    from sqlalchemy import select as _select
    secondary_workspaces = []
    async with AsyncSessionLocal() as _db:
        _rows = (await _db.execute(
            _select(WorkspaceLocation)
            .where(WorkspaceLocation.is_primary == False)  # noqa: E712
            .order_by(WorkspaceLocation.created_at)
        )).scalars().all()
        secondary_workspaces = [
            {"id": r.id, "path": r.path, "label": r.label, "writable": r.writable}
            for r in _rows
        ]

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "google_connected": google_connected,
            "google_configured": google_configured,
            "fernet_configured": fernet_configured,
            "workspace_dir": str(app_config.WORKSPACE_DIR),
            "secondary_workspaces": secondary_workspaces,
            "available_models": list(AVAILABLE_MODELS),
            "default_thread_model": app_config.DEFAULT_THREAD_MODEL,
            "telegram_bot_token": app_config.TELEGRAM_BOT_TOKEN,
            "telegram_chat_id": app_config.TELEGRAM_CHAT_ID,
            "telegram_webhook_url": app_config.TELEGRAM_WEBHOOK_URL,
            "telegram_webhook_active": bool(app_config.TELEGRAM_WEBHOOK_URL and app_config.TELEGRAM_WEBHOOK_SECRET),
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

    # Upsert the primary WorkspaceLocation row in the DB
    from sqlalchemy import select as _select
    resolved_str = str(new_path)
    async with AsyncSessionLocal() as _db:
        current_primary = (await _db.execute(
            _select(WorkspaceLocation).where(WorkspaceLocation.is_primary == True)  # noqa: E712
        )).scalar_one_or_none()
        if current_primary:
            current_primary.path = resolved_str
            current_primary.label = "Main workspace"
        else:
            _db.add(WorkspaceLocation(
                path=resolved_str, label="Main workspace",
                is_primary=True, writable=True,
            ))
        await _db.commit()

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


@router.post("/default-model")
async def update_default_model(
    model: str = Form(...),
    _user: User = Depends(require_user),
):
    """Save the default model for new threads. Updates live config + persists to .env."""
    import app.config as app_config
    from app.web.routes.chat import AVAILABLE_MODELS

    model = model.strip()
    if model not in AVAILABLE_MODELS:
        return RedirectResponse(url="/settings?model_error=invalid", status_code=302)

    app_config.DEFAULT_THREAD_MODEL = model
    _update_env_var("DEFAULT_THREAD_MODEL", model)
    return RedirectResponse(url="/settings?default_model_ok=1", status_code=302)


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


@router.post("/telegram/webhook")
async def register_telegram_webhook(
    request: Request,
    _user: User = Depends(require_user),
):
    """Register the Telegram webhook with the given ngrok base URL."""
    import secrets
    import httpx
    import app.config as app_config

    form = await request.form()
    webhook_url = form.get("webhook_url", "").strip().rstrip("/")

    if not webhook_url:
        return RedirectResponse(url="/settings?webhook_error=empty", status_code=302)

    token = app_config.TELEGRAM_BOT_TOKEN
    if not token:
        return RedirectResponse(url="/settings?webhook_error=no_token", status_code=302)

    # Generate secret if not already set
    secret = app_config.TELEGRAM_WEBHOOK_SECRET or secrets.token_hex(32)

    full_url = webhook_url + "/telegram/webhook"

    # Call Telegram setWebhook
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": full_url, "secret_token": secret},
            )
            data = resp.json()
            if not data.get("ok"):
                error_msg = data.get("description", "Unknown error")
                return RedirectResponse(
                    url=f"/settings?webhook_error={error_msg[:80]}", status_code=302
                )
    except Exception as exc:
        return RedirectResponse(url=f"/settings?webhook_error={str(exc)[:80]}", status_code=302)

    # Update live config
    app_config.TELEGRAM_WEBHOOK_URL = webhook_url
    app_config.TELEGRAM_WEBHOOK_SECRET = secret

    # Persist to .env
    env_path = BASE_DIR.parent / ".env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updates = {
            "TELEGRAM_WEBHOOK_URL": webhook_url,
            "TELEGRAM_WEBHOOK_SECRET": secret,
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
        for key, val in updates.items():
            if key not in found:
                new_lines.append(f"{key}={val}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return RedirectResponse(url="/settings?webhook_ok=1", status_code=302)


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
    """Delete all messages and threads, reset ID sequences, clear LangGraph checkpoints."""
    import logging
    import app.config as app_config
    from sqlalchemy import text
    from app.db.engine import AsyncSessionLocal

    logger = logging.getLogger(__name__)

    await db.execute(delete(Message))
    await db.execute(delete(Thread))
    await db.commit()

    # Run sequence reset + checkpoint cleanup in a fresh connection
    # so DDL (ALTER SEQUENCE) doesn't conflict with the delete transaction.
    try:
        url = app_config.DATABASE_URL
        async with AsyncSessionLocal() as conn:
            if "postgresql" in url:
                for seq in ("threads_id_seq", "messages_id_seq"):
                    try:
                        await conn.execute(text(f"ALTER SEQUENCE {seq} RESTART WITH 1"))
                    except Exception as exc:
                        logger.warning("clear_chats: reset seq %s: %s", seq, exc)
                for tbl in ("checkpoints", "checkpoint_blobs", "checkpoint_migrations", "checkpoint_writes"):
                    try:
                        await conn.execute(text(f"DELETE FROM {tbl}"))
                    except Exception:
                        pass
            else:
                try:
                    await conn.execute(text("DELETE FROM sqlite_sequence WHERE name IN ('threads', 'messages')"))
                except Exception:
                    pass
                for tbl in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    try:
                        await conn.execute(text(f"DELETE FROM {tbl}"))
                    except Exception:
                        pass
            await conn.commit()
    except Exception as exc:
        logger.warning("clear_chats: post-commit cleanup failed: %s", exc)

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

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(OAuthToken).where(OAuthToken.provider == "google").limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.token_json = encrypted
            existing.refreshed_at = now
        else:
            db.add(OAuthToken(provider="google", token_json=encrypted, refreshed_at=now))
        await db.commit()

    response = RedirectResponse(url="/settings?connected=1", status_code=302)
    response.delete_cookie("google_oauth_state")
    return response


@router.post("/google/disconnect")
async def google_disconnect(
    _user: User = Depends(require_user),
):
    """Remove the stored Google OAuth token."""
    async with AsyncSessionLocal() as db:
        await db.execute(delete(OAuthToken).where(OAuthToken.provider == "google"))
        await db.commit()
    return RedirectResponse(url="/settings?disconnected=1", status_code=302)
