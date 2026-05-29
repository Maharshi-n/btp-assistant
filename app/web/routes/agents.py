"""Agents UI + API: create (interview→finalize), list, pause/resume, runs, memory."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.agents.agent_parser import finalize_agent_spec, interview_questions
from app.agents.agent_memory import agent_lock, _atomic_write, load_memory_file
from app.automations.runtime import register_agent_trigger, unregister_agent_trigger
from app.db.engine import get_db
from app.db.models import Agent, AgentRun, AgentTrigger, Thread, User
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
        _atomic_write(path, "## Durable facts\n\n## Open tasks\n\n## Recent decisions\n\n## People/contacts\n\n<!-- MANUAL_NOTES_BEGIN -->\n<!-- MANUAL_NOTES_END -->\n")


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


async def delete_agent_record(db: AsyncSession, agent_id: int) -> None:
    """Fully remove an agent: unregister its scheduler triggers, delete its
    triggers + runs + the agent row, and remove its memory file."""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")

    # Unregister any live scheduler jobs first so they can't fire for a dead agent.
    trigs = (await db.execute(
        select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
    )).scalars().all()
    for t in trigs:
        try:
            unregister_agent_trigger(t.id)
        except Exception as exc:
            logger.warning("delete_agent_record: unregister trigger %s failed: %s", t.id, exc)

    # Remove the memory file (best-effort).
    try:
        path = app_config.WORKSPACE_DIR / agent.memory_path
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning("delete_agent_record: could not remove memory file for %s: %s", agent_id, exc)

    # Delete dependent rows then the agent. AgentRun.trigger_id FKs the triggers,
    # so delete runs before triggers.
    await db.execute(sa_delete(AgentRun).where(AgentRun.agent_id == agent_id))
    await db.execute(sa_delete(AgentTrigger).where(AgentTrigger.agent_id == agent_id))
    await db.delete(agent)
    await db.commit()


@router.get("/agents")
async def agents_page(request: Request, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    agents = (await db.execute(select(Agent).order_by(Agent.created_at.desc()))).scalars().all()
    cards = []
    for a in agents:
        last = (await db.execute(
            select(AgentRun).where(AgentRun.agent_id == a.id).order_by(AgentRun.started_at.desc()).limit(1)
        )).scalars().first()
        trigs = (await db.execute(
            select(AgentTrigger).where(AgentTrigger.agent_id == a.id)
        )).scalars().all()
        next_wake = None
        for t in trigs:
            if t.trigger_type == "self_scheduled":
                fa = json.loads(t.trigger_config_json).get("fire_at")
                if fa and (next_wake is None or fa < next_wake):
                    next_wake = fa
        cards.append({
            "agent": a,
            "last_status": last.status if last else None,
            "last_summary": (last.trigger_summary or "")[:120] if last else "",
            "last_at": last.started_at.isoformat() if last and last.started_at else None,
            "next_wake": next_wake,
            "trigger_count": len(trigs),
        })
    return templates.TemplateResponse("agents.html", {"request": request, "cards": cards})


@router.get("/agents/{agent_id}")
async def agent_detail_page(agent_id: int, request: Request, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    triggers = (await db.execute(
        select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
    )).scalars().all()
    runs = (await db.execute(
        select(AgentRun).where(AgentRun.agent_id == agent_id).order_by(AgentRun.started_at.desc()).limit(50)
    )).scalars().all()
    path = app_config.WORKSPACE_DIR / agent.memory_path
    memory = load_memory_file(path)
    from app.agents.agent_episodes import read_episodes
    episodes = read_episodes(app_config.WORKSPACE_DIR / "agents" / f"{agent_id}_episodes.jsonl")
    return templates.TemplateResponse("agent_detail.html", {
        "request": request,
        "agent": agent,
        "triggers": triggers,
        "runs": runs,
        "memory": memory,
        "episodes": list(reversed(episodes))[:50],
    })


@router.post("/api/agents/{agent_id}/chat")
async def api_new_agent_chat(agent_id: int, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    """Create a fresh chat thread linked to this agent and return its id.
    The thread stays visible in the main list; the supervisor overlays the
    agent's persona + memory because the thread carries agent_id."""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    thread = Thread(title=f"Chat with {agent.name[:40]}", model=agent.model, agent_id=agent_id)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return JSONResponse({"thread_id": thread.id})


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


@router.delete("/api/agents/{agent_id}")
async def api_delete_agent(agent_id: int, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    await delete_agent_record(db, agent_id)
    return JSONResponse({"deleted": agent_id})


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


@router.get("/api/agents/{agent_id}/triggers")
async def api_list_triggers(agent_id: int, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    trigs = (await db.execute(
        select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
    )).scalars().all()
    return JSONResponse([
        {"id": t.id, "trigger_type": t.trigger_type,
         "config": json.loads(t.trigger_config_json), "created_by": t.created_by or "user",
         "enabled": t.enabled}
        for t in trigs
    ])


@router.post("/api/agents/{agent_id}/triggers")
async def api_create_trigger(agent_id: int, request: Request, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    body = await request.json()
    tt = body.get("trigger_type")
    cfg = body.get("trigger_config", {})
    from app.agents.agent_parser import validate_triggers
    try:
        validate_triggers([{"trigger_type": tt, "trigger_config": cfg}])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    trig = AgentTrigger(agent_id=agent_id, trigger_type=tt,
                        trigger_config_json=json.dumps(cfg), created_by="user")
    db.add(trig)
    await db.commit()
    await db.refresh(trig)
    register_agent_trigger(trig, loop=None)
    return JSONResponse({"id": trig.id, "trigger_type": trig.trigger_type})


@router.delete("/api/agents/{agent_id}/triggers/{trigger_id}")
async def api_delete_trigger(agent_id: int, trigger_id: int, db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    t = await db.get(AgentTrigger, trigger_id)
    if t is None or t.agent_id != agent_id:
        raise HTTPException(404, "Trigger not found")
    unregister_agent_trigger(t.id)
    await db.delete(t)
    await db.commit()
    return JSONResponse({"deleted": trigger_id})


@router.get("/api/agents/{agent_id}/episodes")
async def api_episodes(agent_id: int, q: str = "", since: str = "", db: AsyncSession = Depends(get_db), _u: User = Depends(require_user)):
    from app.agents.agent_episodes import recall
    path = app_config.WORKSPACE_DIR / "agents" / f"{agent_id}_episodes.jsonl"
    hits = recall(path, query=q, since=since or None)
    return JSONResponse(hits)


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
