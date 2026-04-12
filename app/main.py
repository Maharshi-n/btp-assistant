from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.supervisor import init_supervisor, shutdown_supervisor
from app.automations.runtime import start_automations_runtime, stop_automations_runtime
import app.config as app_config
from app.db.engine import get_db, init_db
from app.db.models import Automation, AutomationRun, Message, OAuthToken, TelegramPendingReply, Thread, User  # noqa: F401 — ensures models are registered
from app.db.seed import seed_admin
from app.web.deps import NotAuthenticated, require_user
from app.web.routes.audit import router as audit_router
from app.web.routes.auth import router as auth_router
from app.web.routes.automations import router as automations_router
from app.web.routes.telegram import router as telegram_router
from app.web.routes.chat import AVAILABLE_MODELS, router as chat_router
from app.web.routes.health import router as health_router
from app.web.routes.permissions import router as permissions_router
from app.web.routes.settings import router as settings_router
from app.web.routes.ws import router as ws_router

BASE_DIR = Path(__file__).resolve().parent

# Suppress noisy Windows WebSocket disconnect errors — these are normal when
# the browser closes a tab or a phone screen locks mid-connection.
logging.getLogger("websockets.legacy.protocol").setLevel(logging.CRITICAL)
logging.getLogger("uvicorn.error").addFilter(
    type("_WSFilter", (logging.Filter,), {
        "filter": staticmethod(lambda r: "data transfer failed" not in r.getMessage()
                               and "WinError 121" not in r.getMessage())
    })()
)

app = FastAPI(title="RAION")

# Static files
app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "web" / "static"),
    name="static",
)

# Templates
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

# Routers
app.include_router(health_router)
app.include_router(audit_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(permissions_router)
app.include_router(settings_router)
app.include_router(ws_router)
app.include_router(automations_router)
app.include_router(telegram_router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=302)


@app.on_event("startup")
async def on_startup() -> None:
    app_config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    await seed_admin()
    await init_supervisor()
    await start_automations_runtime()
    await _reregister_telegram_webhook()


async def _reregister_telegram_webhook() -> None:
    """Re-register Telegram webhook on startup (ngrok URL may have changed)."""
    token = app_config.TELEGRAM_BOT_TOKEN
    webhook_url = app_config.TELEGRAM_WEBHOOK_URL
    secret = app_config.TELEGRAM_WEBHOOK_SECRET
    if not token or not webhook_url or not secret:
        return
    full_url = webhook_url.rstrip("/") + "/telegram/webhook"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": full_url, "secret_token": secret},
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                logging.getLogger(__name__).info(
                    "Telegram webhook re-registered: %s", full_url
                )
            else:
                logging.getLogger(__name__).warning(
                    "Telegram webhook re-registration failed: %s", resp.text[:200]
                )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Telegram webhook re-registration error: %s", exc
        )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await stop_automations_runtime()
    await shutdown_supervisor()


@app.get("/")
async def index(
    request: Request,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Thread).order_by(Thread.created_at.desc()))
    threads = result.scalars().all()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "threads": threads,
            "available_models": AVAILABLE_MODELS,
        },
    )
