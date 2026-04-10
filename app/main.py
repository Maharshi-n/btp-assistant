from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db.engine import init_db
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


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
