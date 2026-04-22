"""User memory routes.

Endpoints:
  GET    /memory              → memory page (HTML)
  POST   /api/memory          → add a memory entry (JSON: {content})
  DELETE /api/memory/:id      → delete a memory entry
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import AutoMemoryConfig, UserMemory
from app.web.deps import require_user
from app.db.models import User

BASE_DIR = Path(__file__).resolve().parent.parent.parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")


async def _get_auto_memory_enabled(db: AsyncSession) -> bool:
    result = await db.execute(select(AutoMemoryConfig).limit(1))
    cfg = result.scalars().first()
    return cfg.enabled if cfg else False


@router.get("/memory")
async def memory_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(
        select(UserMemory).order_by(UserMemory.created_at.desc())
    )
    memories = result.scalars().all()
    auto_enabled = await _get_auto_memory_enabled(db)
    return templates.TemplateResponse(
        "memory.html",
        {"request": request, "memories": memories, "auto_memory_enabled": auto_enabled},
    )


@router.get("/api/memory/auto-config")
async def get_auto_memory_config(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    enabled = await _get_auto_memory_enabled(db)
    return {"enabled": enabled}


@router.post("/api/memory/auto-config")
async def set_auto_memory_config(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    enabled = bool(payload.get("enabled", False))
    result = await db.execute(select(AutoMemoryConfig).limit(1))
    cfg = result.scalars().first()
    if cfg:
        cfg.enabled = enabled
    else:
        db.add(AutoMemoryConfig(enabled=enabled))
    await db.commit()
    return {"enabled": enabled}


@router.post("/api/memory")
async def add_memory(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    content: str = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content is required")

    entry = UserMemory(content=content)
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    from app.agents.supervisor import invalidate_memory_cache
    invalidate_memory_cache()

    return {
        "id": entry.id,
        "content": entry.content,
        "created_at": entry.created_at.isoformat(),
    }


@router.delete("/api/memory/{memory_id}")
async def delete_memory(
    memory_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    entry = await db.get(UserMemory, memory_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    await db.delete(entry)
    await db.commit()

    from app.agents.supervisor import invalidate_memory_cache
    invalidate_memory_cache()

    return {"deleted": True}
