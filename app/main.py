from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.supervisor import init_supervisor, shutdown_supervisor
import app.config as app_config
from app.db.engine import get_db, init_db
from app.db.models import Message, Thread, User  # noqa: F401 — ensures models are registered
from app.db.seed import seed_admin
from app.web.deps import NotAuthenticated, require_user
from app.web.routes.auth import router as auth_router
from app.web.routes.chat import AVAILABLE_MODELS, router as chat_router
from app.web.routes.health import router as health_router
from app.web.routes.permissions import router as permissions_router
from app.web.routes.ws import router as ws_router

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="BTP Personal AI Assistant")

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
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(permissions_router)
app.include_router(ws_router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=302)


@app.on_event("startup")
async def on_startup() -> None:
    app_config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    await seed_admin()
    await init_supervisor()


@app.on_event("shutdown")
async def on_shutdown() -> None:
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
