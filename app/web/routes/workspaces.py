"""Workspace locations CRUD API."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.db.engine import get_db
from app.db.models import User, WorkspaceLocation
from app.web.deps import require_user

router = APIRouter(prefix="/api/workspaces")


class LocationCreate(BaseModel):
    path: str
    label: str
    writable: bool = True


class LocationUpdate(BaseModel):
    label: str | None = None
    writable: bool | None = None


def _resolve_path(path_str: str) -> Path:
    p = Path(path_str.strip()).resolve()
    return p


@router.get("")
async def list_workspaces(
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(WorkspaceLocation).order_by(
                WorkspaceLocation.is_primary.desc(),
                WorkspaceLocation.created_at,
            )
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "path": r.path,
            "label": r.label,
            "is_primary": r.is_primary,
            "writable": r.writable,
        }
        for r in rows
    ]


@router.post("")
async def add_workspace(
    body: LocationCreate,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    new_path = _resolve_path(body.path)
    if not new_path.exists():
        try:
            new_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot create directory: {e}")
    if not new_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory.")

    resolved_str = str(os.path.realpath(str(new_path)))

    existing = (
        await db.execute(
            select(WorkspaceLocation).where(WorkspaceLocation.path == resolved_str)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="This path is already a workspace location.")

    loc = WorkspaceLocation(
        path=resolved_str,
        label=body.label.strip() or new_path.name,
        is_primary=False,
        writable=body.writable,
    )
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return {"id": loc.id, "path": loc.path, "label": loc.label, "is_primary": False, "writable": loc.writable}


@router.patch("/{loc_id}")
async def update_workspace(
    loc_id: int,
    body: LocationUpdate,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    loc = (
        await db.execute(select(WorkspaceLocation).where(WorkspaceLocation.id == loc_id))
    ).scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Workspace location not found.")
    if body.label is not None:
        loc.label = body.label.strip()
    if body.writable is not None:
        loc.writable = body.writable
    await db.commit()
    return {"ok": True}


@router.delete("/{loc_id}")
async def delete_workspace(
    loc_id: int,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    loc = (
        await db.execute(select(WorkspaceLocation).where(WorkspaceLocation.id == loc_id))
    ).scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Workspace location not found.")
    if loc.is_primary:
        raise HTTPException(status_code=400, detail="Cannot delete the primary workspace. Set another location as primary first.")
    await db.delete(loc)
    await db.commit()
    return {"ok": True}


@router.post("/{loc_id}/set-primary")
async def set_primary_workspace(
    loc_id: int,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    loc = (
        await db.execute(select(WorkspaceLocation).where(WorkspaceLocation.id == loc_id))
    ).scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Workspace location not found.")

    # Demote current primary
    current_primary = (
        await db.execute(
            select(WorkspaceLocation).where(WorkspaceLocation.is_primary == True)  # noqa: E712
        )
    ).scalar_one_or_none()
    if current_primary:
        current_primary.is_primary = False

    loc.is_primary = True
    await db.commit()

    # Update live config + .env
    new_path = Path(loc.path)
    app_config.WORKSPACE_DIR = new_path

    env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
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

    return {"ok": True, "path": str(new_path)}
