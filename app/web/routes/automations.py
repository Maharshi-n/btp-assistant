"""Phase 9: Automations web routes.

Endpoints:
  GET  /automations           → list page (HTML)
  POST /api/automations       → create (JSON body: {nl_description})
  POST /api/automations/:id/enable   → enable
  POST /api/automations/:id/disable  → disable
  DELETE /api/automations/:id        → delete
  GET  /api/automations/:id/runs     → recent runs list (JSON)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.parser import parse_automation
from app.automations.runtime import (
    disable_automation,
    enable_automation,
    register_new_automation,
    unregister_automation,
)
from app.db.engine import get_db
from app.db.models import Automation, AutomationRun, Thread
from app.web.deps import require_user
from app.db.models import User

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@router.get("/automations")
async def automations_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(
        select(Automation).order_by(Automation.created_at.desc())
    )
    automations = result.scalars().all()

    # For each automation, get the count of runs and last run status
    automation_data = []
    for a in automations:
        runs_result = await db.execute(
            select(AutomationRun)
            .where(AutomationRun.automation_id == a.id)
            .order_by(AutomationRun.started_at.desc())
            .limit(1)
        )
        last_run = runs_result.scalars().first()
        automation_data.append({
            "automation": a,
            "last_run": last_run,
            "trigger_config": json.loads(a.trigger_config_json),
        })

    from app.web.routes.chat import AVAILABLE_MODELS
    return templates.TemplateResponse(
        "automations.html",
        {
            "request": request,
            "automation_data": automation_data,
            "available_models": list(AVAILABLE_MODELS),
        },
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@router.post("/api/automations")
async def create_automation(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    nl_description: str = payload.get("nl_description", "").strip()
    custom_name: str = payload.get("name", "").strip()
    model: str = payload.get("model", "gpt-4o-mini").strip() or "gpt-4o-mini"
    if not nl_description:
        raise HTTPException(status_code=422, detail="nl_description is required")

    try:
        parsed = await parse_automation(nl_description, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Error parsing automation: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to parse automation description")

    automation = Automation(
        name=custom_name if custom_name else parsed["name"],
        raw_description=nl_description,
        trigger_type=parsed["trigger_type"],
        trigger_config_json=json.dumps(parsed["trigger_config"]),
        action_prompt=parsed["action_prompt"],
        model=model,
        enabled=True,
    )
    db.add(automation)
    await db.commit()
    await db.refresh(automation)

    # Register the trigger immediately
    await register_new_automation(automation)

    return {
        "id": automation.id,
        "name": automation.name,
        "trigger_type": automation.trigger_type,
        "trigger_config": parsed["trigger_config"],
        "action_prompt": automation.action_prompt,
        "enabled": automation.enabled,
        "created_at": automation.created_at.isoformat(),
    }


@router.patch("/api/automations/{automation_id}")
async def update_automation(
    automation_id: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    automation = await db.get(Automation, automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="Automation not found")
    if "model" in payload:
        automation.model = payload["model"].strip() or "gpt-4o-mini"
    await db.commit()
    return {"id": automation.id, "model": automation.model}


@router.post("/api/automations/{automation_id}/edit")
async def edit_automation(
    automation_id: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    """Re-parse an automation from an updated name and/or description."""
    automation = await db.get(Automation, automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="Automation not found")

    new_name: str = payload.get("name", "").strip()
    new_description: str = payload.get("nl_description", "").strip()

    if not new_description:
        raise HTTPException(status_code=422, detail="nl_description is required")

    try:
        parsed = await parse_automation(new_description, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Error re-parsing automation: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to parse automation description")

    # Use manually provided name if given, otherwise use parser's name
    automation.name = new_name if new_name else parsed["name"]
    automation.raw_description = new_description
    automation.trigger_type = parsed["trigger_type"]
    automation.trigger_config_json = json.dumps(parsed["trigger_config"])
    automation.action_prompt = parsed["action_prompt"]

    await db.commit()

    # Re-register the trigger with new config
    unregister_automation(automation_id)
    await db.refresh(automation)
    await register_new_automation(automation)

    return {
        "id": automation.id,
        "name": automation.name,
        "trigger_type": automation.trigger_type,
        "trigger_config": parsed["trigger_config"],
        "action_prompt": automation.action_prompt,
        "enabled": automation.enabled,
    }


@router.post("/api/automations/{automation_id}/enable")
async def enable_automation_route(
    automation_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    automation = await db.get(Automation, automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="Automation not found")

    automation.enabled = True
    await db.commit()
    await db.refresh(automation)

    await enable_automation(automation)
    return {"id": automation.id, "enabled": True}


@router.post("/api/automations/{automation_id}/disable")
async def disable_automation_route(
    automation_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    automation = await db.get(Automation, automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="Automation not found")

    automation.enabled = False
    await db.commit()

    await disable_automation(automation_id)
    return {"id": automation_id, "enabled": False}


@router.delete("/api/automations/{automation_id}")
async def delete_automation_route(
    automation_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    automation = await db.get(Automation, automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="Automation not found")

    unregister_automation(automation_id)
    # Delete child rows first to avoid FK violations
    from app.db.models import AutomationConversation
    await db.execute(
        delete(AutomationRun).where(AutomationRun.automation_id == automation_id)
    )
    await db.execute(
        delete(AutomationConversation).where(AutomationConversation.automation_id == automation_id)
    )
    await db.delete(automation)
    await db.commit()
    return {"deleted": True}


@router.get("/api/automations/{automation_id}/runs")
async def get_automation_runs(
    automation_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    automation = await db.get(Automation, automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="Automation not found")

    result = await db.execute(
        select(AutomationRun)
        .where(AutomationRun.automation_id == automation_id)
        .order_by(AutomationRun.started_at.desc())
        .limit(20)
    )
    runs = result.scalars().all()

    return [
        {
            "id": r.id,
            "automation_id": r.automation_id,
            "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "status": r.status,
            "thread_id": r.thread_id,
        }
        for r in runs
    ]
