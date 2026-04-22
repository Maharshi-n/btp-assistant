"""Skills routes.

Endpoints:
  GET    /skills               → skills page (HTML)
  POST   /api/skills           → create skill (multipart: name, trigger_description, file)
  POST   /api/skills/:id/enable  → enable
  POST   /api/skills/:id/disable → disable
  DELETE /api/skills/:id       → delete skill + file
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.db.engine import get_db
from app.db.models import Skill, User
from app.web.deps import require_user

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

# Skills files live here — inside workspace so they survive workspace changes
def _skills_dir() -> Path:
    d = app_config.WORKSPACE_DIR / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@router.get("/skills")
async def skills_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(select(Skill).order_by(Skill.created_at.desc()))
    skills = result.scalars().all()
    return templates.TemplateResponse(
        "skills.html",
        {"request": request, "skills": skills},
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@router.get("/api/skills")
async def list_skills(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    """Return all enabled skills as JSON (for autocomplete)."""
    result = await db.execute(
        select(Skill).where(Skill.enabled == True).order_by(Skill.name)  # noqa: E712
    )
    skills = result.scalars().all()
    return [
        {"id": s.id, "name": s.name, "trigger_description": s.trigger_description}
        for s in skills
    ]


@router.post("/api/skills")
async def create_skill(
    name: str = Form(...),
    trigger_description: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    name = name.strip().lower().replace(" ", "_")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if not trigger_description.strip():
        raise HTTPException(status_code=422, detail="trigger_description is required")

    # Check for duplicate name
    existing = await db.execute(select(Skill).where(Skill.name == name))
    if existing.scalars().first():
        raise HTTPException(status_code=409, detail=f"Skill '{name}' already exists")

    # Save file
    suffix = Path(file.filename).suffix if file.filename else ".txt"
    relative_path = f"skills/{name}{suffix}"
    abs_path = _skills_dir() / f"{name}{suffix}"
    content = await file.read()
    abs_path.write_bytes(content)

    skill = Skill(
        name=name,
        trigger_description=trigger_description.strip(),
        file_path=relative_path,
        enabled=True,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)

    # Invalidate skills cache so supervisor picks up the new skill immediately
    _invalidate_skills_cache()

    logger.info("Created skill '%s' at %s", name, abs_path)
    return {
        "id": skill.id,
        "name": skill.name,
        "trigger_description": skill.trigger_description,
        "file_path": relative_path,
        "enabled": skill.enabled,
        "created_at": skill.created_at.isoformat(),
    }


@router.post("/api/skills/{skill_id}/enable")
async def enable_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    skill.enabled = True
    await db.commit()
    _invalidate_skills_cache()
    return {"id": skill_id, "enabled": True}


@router.post("/api/skills/{skill_id}/disable")
async def disable_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    skill.enabled = False
    await db.commit()
    _invalidate_skills_cache()
    return {"id": skill_id, "enabled": False}


@router.delete("/api/skills/{skill_id}")
async def delete_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    # Delete file from disk (file_path is relative to WORKSPACE_DIR)
    try:
        abs_path = app_config.WORKSPACE_DIR / skill.file_path
        abs_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not delete skill file %s: %s", skill.file_path, exc)

    await db.delete(skill)
    await db.commit()
    _invalidate_skills_cache()
    return {"deleted": True}


def _invalidate_skills_cache() -> None:
    try:
        from app.agents.supervisor import invalidate_skills_cache
        invalidate_skills_cache()
    except Exception:
        pass
