"""Agents UI + API: create (interview→finalize), list, pause/resume, runs, memory."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.agents.agent_parser import finalize_agent_spec, interview_questions
from app.agents.agent_memory import agent_lock, _atomic_write, load_memory_file
from app.automations.runtime import register_agent_trigger, unregister_agent_trigger
from app.db.engine import get_db
from app.db.models import Agent, AgentRun, AgentTrigger, User
from app.web.deps import require_user

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent.parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")


def _memory_path_for(agent_id: int) -> str:
    return f"agents/{agent_id}_memory.md"


def _init_memory_file(agent_id: int) -> None:
    path = app_config.WORKSPACE_DIR / _memory_path_for(agent_id)
    if not path.exists():
        _atomic_write(path, "## Durable facts\n\n## Open tasks\n\n## Recent decisions\n\n## People/contacts\n")


async def create_agent_record(
    db: AsyncSession, name: str, role_description: str, interview_answers: str
) -> Agent:
    spec = await finalize_agent_spec(role_description, interview_answers)
    agent = Agent(
        name=name,
        role_description=role_description,
        role_block=spec["role_block"],
        memory_path="agents/pending.md",
    )
    db.add(agent)
    await db.flush()
    agent.memory_path = _memory_path_for(agent.id)
    triggers = []
    for t in spec["triggers"]:
        trig = AgentTrigger(
            agent_id=agent.id,
            trigger_type=t["trigger_type"],
            trigger_config_json=json.dumps(t.get("trigger_config", {})),
        )
        db.add(trig)
        triggers.append(trig)
    await db.commit()
    await db.refresh(agent)
    _init_memory_file(agent.id)
    for trig in triggers:
        await db.refresh(trig)
        register_agent_trigger(trig, loop=None)
    return agent


async def set_agent_status(db: AsyncSession, agent_id: int, status: str) -> None:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    agent.status = status
    await db.commit()
    trigs = (await db.execute(
        select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
    )).scalars().all()
    if status == "active":
        for t in trigs:
            register_agent_trigger(t, loop=None)
    else:
        for t in trigs:
            unregister_agent_trigger(t.id)


@router.get("/agents")
async def agents_page(request: Request, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    agents = (await db.execute(select(Agent).order_by(Agent.created_at.desc()))).scalars().all()
    return templates.TemplateResponse("agents.html", {"request": request, "agents": agents})


@router.post("/api/agents/interview")
async def api_interview(request: Request, _u: User = Depends(require_user)):
    body = await request.json()
    role = body.get("role_description", "").strip()
    if not role:
        raise HTTPException(400, "role_description required")
    questions = await interview_questions(role)
    return JSONResponse({"questions": questions})


@router.post("/api/agents")
async def api_create_agent(request: Request, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    body = await request.json()
    name = body.get("name", "").strip() or "Unnamed Agent"
    role = body.get("role_description", "").strip()
    answers = body.get("interview_answers", "").strip()
    if not role:
        raise HTTPException(400, "role_description required")
    agent = await create_agent_record(db, name, role, answers)
    return JSONResponse({"id": agent.id, "name": agent.name, "status": agent.status})


@router.post("/api/agents/{agent_id}/pause")
async def api_pause(agent_id: int, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    await set_agent_status(db, agent_id, "paused")
    return JSONResponse({"status": "paused"})


@router.post("/api/agents/{agent_id}/resume")
async def api_resume(agent_id: int, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    await set_agent_status(db, agent_id, "active")
    return JSONResponse({"status": "active"})


@router.get("/api/agents/{agent_id}/runs")
async def api_runs(agent_id: int, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    runs = (await db.execute(
        select(AgentRun).where(AgentRun.agent_id == agent_id).order_by(AgentRun.started_at.desc()).limit(50)
    )).scalars().all()
    return JSONResponse([
        {"id": r.id, "status": r.status, "summary": r.trigger_summary,
         "started_at": r.started_at.isoformat() if r.started_at else None}
        for r in runs
    ])


@router.get("/api/agents/{agent_id}/memory")
async def api_get_memory(agent_id: int, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    path = app_config.WORKSPACE_DIR / agent.memory_path
    return JSONResponse({"memory": load_memory_file(path)})


@router.put("/api/agents/{agent_id}/memory")
async def api_put_memory(agent_id: int, request: Request, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    body = await request.json()
    content = body.get("memory", "")
    path = app_config.WORKSPACE_DIR / agent.memory_path
    async with agent_lock(agent_id):
        _atomic_write(path, content)
    return JSONResponse({"ok": True})
