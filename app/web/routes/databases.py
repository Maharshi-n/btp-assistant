"""Databases routes — external DB connection management.

Endpoints:
  GET    /databases                        → HTML page
  GET    /api/databases                    → list all connections
  POST   /api/databases                    → create connection
  PUT    /api/databases/{id}               → update connection
  DELETE /api/databases/{id}               → delete + remove skill file
  POST   /api/databases/{id}/test          → test connection (no save)
  POST   /api/databases/{id}/scan          → trigger manual scan
  PUT    /api/databases/{id}/description   → update description + regen skill
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.db.engine import get_db
from app.db.models import DBConnection, Skill, User
from app.db_connections.manager import (
    encrypt_credentials,
    normalize_name,
    scan_schema,
    test_connection,
)
from app.web.deps import require_user

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class DBCreate(BaseModel):
    name: str
    db_type: str           # mssql | mysql | postgres | sqlite
    host: str | None = None
    port: int | None = None
    db_name: str
    username: str | None = None
    password: str | None = None
    whitelisted_tables: list[str] = []


class DBUpdate(BaseModel):
    host: str | None = None
    port: int | None = None
    db_name: str | None = None
    username: str | None = None
    password: str | None = None
    whitelisted_tables: list[str] | None = None


class DescriptionUpdate(BaseModel):
    description: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn_dict(conn: DBConnection) -> dict:
    return {
        "id": conn.id,
        "name": conn.name,
        "db_type": conn.db_type,
        "host": conn.host,
        "port": conn.port,
        "db_name": conn.db_name,
        "whitelisted_tables": json.loads(conn.whitelisted_tables or "[]"),
        "skill_description": conn.skill_description,
        "last_scanned_at": conn.last_scanned_at.isoformat() if conn.last_scanned_at else None,
        "is_scanning": conn.is_scanning,
        "is_active": conn.is_active,
        "created_at": conn.created_at.isoformat(),
    }


def _seconds_until_next_scan(conn: DBConnection) -> int | None:
    """Return seconds remaining in cooldown, or None if scan is allowed."""
    if not conn.last_scanned_at:
        return None
    elapsed = (datetime.now(timezone.utc) - conn.last_scanned_at.replace(tzinfo=timezone.utc)).total_seconds()
    remaining = 3600 - elapsed
    return max(0, int(remaining)) if remaining > 0 else None


async def _delete_skill_file(db: AsyncSession, conn_name: str) -> None:
    skill_name = f"db_{conn_name}"
    result = await db.execute(select(Skill).where(Skill.name == skill_name))
    skill = result.scalars().first()
    if skill:
        try:
            fp = app_config.WORKSPACE_DIR / skill.file_path
            if fp.exists():
                fp.unlink()
        except Exception:
            pass
        await db.delete(skill)
    try:
        from app.agents.supervisor import invalidate_skills_cache
        invalidate_skills_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@router.get("/databases")
async def databases_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(select(DBConnection).order_by(DBConnection.created_at.desc()))
    conns = result.scalars().all()
    conn_data = []
    for c in conns:
        d = _conn_dict(c)
        d["scan_cooldown_seconds"] = _seconds_until_next_scan(c)
        conn_data.append(d)
    return templates.TemplateResponse(
        "databases.html",
        {"request": request, "conn_data": conn_data},
    )


# ---------------------------------------------------------------------------
# API: list
# ---------------------------------------------------------------------------

@router.get("/api/databases")
async def list_databases(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(select(DBConnection).order_by(DBConnection.created_at.desc()))
    conns = result.scalars().all()
    out = []
    for c in conns:
        d = _conn_dict(c)
        d["scan_cooldown_seconds"] = _seconds_until_next_scan(c)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# API: create
# ---------------------------------------------------------------------------

@router.post("/api/databases")
async def create_database(
    body: DBCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    try:
        name = normalize_name(body.name)
    except ValueError as e:
        raise HTTPException(422, str(e))

    if body.db_type not in ("mssql", "mysql", "postgres", "sqlite"):
        raise HTTPException(422, "db_type must be mssql, mysql, postgres, or sqlite")

    existing = await db.execute(select(DBConnection).where(DBConnection.name == name))
    if existing.scalars().first():
        raise HTTPException(409, f"Database connection '{name}' already exists")

    username_enc, password_enc = encrypt_credentials(body.username, body.password)

    conn = DBConnection(
        name=name,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        db_name=body.db_name,
        username_enc=username_enc,
        password_enc=password_enc,
        whitelisted_tables=json.dumps(body.whitelisted_tables),
        is_active=True,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)

    import asyncio
    asyncio.create_task(scan_schema(conn.id, force=True))

    return _conn_dict(conn)


# ---------------------------------------------------------------------------
# API: update
# ---------------------------------------------------------------------------

@router.put("/api/databases/{conn_id}")
async def update_database(
    conn_id: int,
    body: DBUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    conn = await db.get(DBConnection, conn_id)
    if not conn:
        raise HTTPException(404, "Database connection not found")

    if body.host is not None:
        conn.host = body.host
    if body.port is not None:
        conn.port = body.port
    if body.db_name is not None:
        conn.db_name = body.db_name
    if body.username is not None or body.password is not None:
        username_enc, password_enc = encrypt_credentials(body.username, body.password)
        if body.username is not None:
            conn.username_enc = username_enc
        if body.password is not None:
            conn.password_enc = password_enc
    if body.whitelisted_tables is not None:
        conn.whitelisted_tables = json.dumps(body.whitelisted_tables)

    await db.commit()
    await db.refresh(conn)
    return _conn_dict(conn)


# ---------------------------------------------------------------------------
# API: delete
# ---------------------------------------------------------------------------

@router.delete("/api/databases/{conn_id}")
async def delete_database(
    conn_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    conn = await db.get(DBConnection, conn_id)
    if not conn:
        raise HTTPException(404, "Database connection not found")
    await _delete_skill_file(db, conn.name)
    await db.delete(conn)
    await db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# API: test connection
# ---------------------------------------------------------------------------

@router.post("/api/databases/{conn_id}/test")
async def test_database(
    conn_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    conn = await db.get(DBConnection, conn_id)
    if not conn:
        raise HTTPException(404, "Database connection not found")
    ok, error = await test_connection(conn)
    return {"ok": ok, "error": error}


# ---------------------------------------------------------------------------
# API: manual scan
# ---------------------------------------------------------------------------

@router.post("/api/databases/{conn_id}/scan")
async def trigger_scan(
    conn_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    conn = await db.get(DBConnection, conn_id)
    if not conn:
        raise HTTPException(404, "Database connection not found")

    if conn.is_scanning:
        return JSONResponse(status_code=200, content={"ok": False, "reason": "Scan already in progress"})

    import asyncio
    asyncio.create_task(scan_schema(conn_id, force=True))
    return {"ok": True, "reason": "Scan started"}


# ---------------------------------------------------------------------------
# API: update description
# ---------------------------------------------------------------------------

@router.put("/api/databases/{conn_id}/description")
async def update_description(
    conn_id: int,
    body: DescriptionUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    conn = await db.get(DBConnection, conn_id)
    if not conn:
        raise HTTPException(404, "Database connection not found")
    conn.skill_description = body.description
    await db.commit()
    await db.refresh(conn)
    import asyncio
    asyncio.create_task(scan_schema(conn_id, force=True))
    return _conn_dict(conn)
