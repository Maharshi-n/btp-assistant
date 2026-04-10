from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db.engine import init_db
from app.db.models import User  # noqa: F401 — ensures models are registered
from app.db.seed import seed_admin
from app.web.deps import NotAuthenticated, require_user
from app.web.routes.auth import router as auth_router
from app.web.routes.health import router as health_router

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


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=302)


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
    await seed_admin()


@app.get("/")
async def index(request: Request, _user: User = Depends(require_user)):
    return templates.TemplateResponse("index.html", {"request": request})
