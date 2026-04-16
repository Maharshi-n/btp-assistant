"""Telegram Commands routes.

Endpoints:
  GET    /telegram-commands              → commands page (HTML)
  POST   /api/telegram-commands          → create command
  POST   /api/telegram-commands/:id/enable  → enable
  POST   /api/telegram-commands/:id/disable → disable
  DELETE /api/telegram-commands/:id      → delete command
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import TelegramCommand, User
from app.web.deps import require_user

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

# Built-in command names that cannot be overridden
_RESERVED_NAMES = {
    "newthread", "thread", "model", "help", "remember", "memory", "ls", "remind",
}

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def _validate_name(name: str) -> str:
    """Normalise and validate a command name. Returns clean name or raises HTTPException."""
    name = name.strip().lstrip("/").lower().replace(" ", "_")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="name must contain only lowercase letters, digits, and underscores",
        )
    if name in _RESERVED_NAMES:
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' is a reserved built-in command. Reserved names: {', '.join(sorted(_RESERVED_NAMES))}",
        )
    return name


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@router.get("/telegram-commands")
async def telegram_commands_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(
        select(TelegramCommand).order_by(TelegramCommand.created_at.desc())
    )
    commands = result.scalars().all()
    return templates.TemplateResponse(
        "telegram_commands.html",
        {"request": request, "commands": commands},
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@router.post("/api/telegram-commands")
async def create_telegram_command(
    name: str = Form(...),
    description: str = Form(...),
    preset_prompt: str = Form(""),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    name = _validate_name(name)

    if not description.strip():
        raise HTTPException(status_code=422, detail="description is required")

    existing = await db.execute(
        select(TelegramCommand).where(TelegramCommand.name == name)
    )
    if existing.scalars().first():
        raise HTTPException(status_code=409, detail=f"Command '/{name}' already exists")

    cmd = TelegramCommand(
        name=name,
        description=description.strip(),
        preset_prompt=preset_prompt.strip() or None,
        enabled=True,
    )
    db.add(cmd)
    await db.commit()
    await db.refresh(cmd)

    logger.info("Created Telegram command '/%s'", name)
    return {
        "id": cmd.id,
        "name": cmd.name,
        "description": cmd.description,
        "preset_prompt": cmd.preset_prompt,
        "enabled": cmd.enabled,
        "created_at": cmd.created_at.isoformat(),
    }


@router.post("/api/telegram-commands/{cmd_id}/enable")
async def enable_telegram_command(
    cmd_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    cmd = await db.get(TelegramCommand, cmd_id)
    if cmd is None:
        raise HTTPException(status_code=404, detail="Command not found")
    cmd.enabled = True
    await db.commit()
    return {"id": cmd_id, "enabled": True}


@router.post("/api/telegram-commands/{cmd_id}/disable")
async def disable_telegram_command(
    cmd_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    cmd = await db.get(TelegramCommand, cmd_id)
    if cmd is None:
        raise HTTPException(status_code=404, detail="Command not found")
    cmd.enabled = False
    await db.commit()
    return {"id": cmd_id, "enabled": False}


@router.delete("/api/telegram-commands/{cmd_id}")
async def delete_telegram_command(
    cmd_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    cmd = await db.get(TelegramCommand, cmd_id)
    if cmd is None:
        raise HTTPException(status_code=404, detail="Command not found")
    await db.delete(cmd)
    await db.commit()
    logger.info("Deleted Telegram command id=%d", cmd_id)
    return {"deleted": True}
