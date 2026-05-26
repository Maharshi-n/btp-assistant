"""fire_agent: wake an agent on a trigger, run it in a fresh thread, log the run,
and schedule debounced memory distillation. Mirrors app/automations/runtime._fire_automation
but adds agent identity, memory, budget, and a per-agent lock.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import app.config as app_config
from app.agents.agent_memory import agent_lock, distil_memory, load_memory_file
from app.agents.agent_prompt import build_agent_prompt
from app.db.engine import AsyncSessionLocal
from app.db.models import Agent, AgentRun, Message, Thread

logger = logging.getLogger(__name__)

# Inactivity-debounce for memory distillation: agent_id -> TimerHandle
_memory_timers: dict[int, asyncio.TimerHandle] = {}
MEMORY_DEBOUNCE_SECONDS = 45


def _session_factory():
    """Indirection so tests can inject a single shared session."""
    return AsyncSessionLocal()


async def _invoke_graph(prompt: str, model: str, lg_thread_id: str, ws_thread_id: int) -> str:
    """Run the existing supervisor graph and return the final assistant text."""
    from langchain_core.messages import AIMessage, HumanMessage
    from app.agents.supervisor import get_graph

    graph = get_graph()
    lg_config = {
        "recursion_limit": 100,
        "configurable": {
            "thread_id": lg_thread_id,
            "ws_thread_id": ws_thread_id,
            "model": model,
            "automation_run": True,  # skip interrupt() — no UI to approve
        },
    }
    full: list[str] = []
    last_ai = ""
    async for event in graph.astream_events(
        {"messages": [HumanMessage(content=prompt)]}, lg_config, version="v2"
    ):
        et = event.get("event", "")
        if et == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk and chunk.content:
                full.append(chunk.content)
        elif et == "on_chain_end" and event.get("name") == "supervisor":
            output = event.get("data", {}).get("output", {})
            msgs = output.get("messages", []) if isinstance(output, dict) else []
            for m in reversed(msgs):
                if isinstance(m, AIMessage) and m.content:
                    last_ai = m.content if isinstance(m.content, str) else str(m.content)
                    break
    return "".join(full) or last_ai


def _resolve_memory_path(memory_path: str) -> Path:
    p = Path(memory_path)
    return p if p.is_absolute() else app_config.WORKSPACE_DIR / p


async def fire_agent(agent_id: int, trigger_id: int | None, trigger_context: dict) -> str:
    """Wake an agent. Returns one of: 'done', 'failed', 'skipped:paused', 'skipped:budget'."""
    async with agent_lock(agent_id):
        async with _session_factory() as db:
            agent = await db.get(Agent, agent_id)
            if agent is None:
                return "skipped:missing"
            if agent.status != "active":
                return "skipped:paused"
            if agent.fires_today >= agent.daily_fire_budget:
                logger.warning("Agent %d over daily budget (%d)", agent_id, agent.daily_fire_budget)
                return "skipped:budget"

            # Fresh thread per fire — the agent never reads its own prior output.
            thread = Thread(title=f"[Agent] {agent.name[:50]}", model=agent.model)
            db.add(thread)
            await db.flush()

            run = AgentRun(
                agent_id=agent_id,
                trigger_id=trigger_id,
                started_at=datetime.now(timezone.utc),
                status="running",
                thread_id=thread.id,
            )
            db.add(run)
            agent.fires_today += 1
            await db.commit()
            await db.refresh(thread)
            await db.refresh(run)

            model = agent.model
            role_block = agent.role_block
            mem_path = _resolve_memory_path(agent.memory_path)
            thread_id = thread.id
            run_id = run.id

        from app.agents.supervisor import _supervisor_system_prompt
        trigger_block = trigger_context.get("trusted_block", "") if trigger_context else ""
        memory_text = load_memory_file(mem_path)
        prompt = build_agent_prompt(
            base_prompt=_supervisor_system_prompt(),
            role_block=role_block,
            memory_text=memory_text,
            trigger_context=trigger_block,
        )

        status = "done"
        final_content = ""
        try:
            final_content = await _invoke_graph(
                prompt=prompt,
                model=model,
                lg_thread_id=f"agent_{agent_id}_{run_id}",
                ws_thread_id=thread_id,
            )
        except Exception as exc:
            logger.exception("Agent %d run %d failed: %s", agent_id, run_id, exc)
            status = "failed"

        async with _session_factory() as db:
            if final_content:
                db.add(Message(
                    thread_id=thread_id,
                    role="assistant",
                    content=final_content,
                    metadata_json=json.dumps({"agent_id": agent_id}),
                ))
            run_obj = await db.get(AgentRun, run_id)
            if run_obj is not None:
                run_obj.finished_at = datetime.now(timezone.utc)
                run_obj.status = status
                run_obj.trigger_summary = (final_content or "")[:200]
            await db.commit()

        schedule_memory_distillation(agent_id, mem_path, thread_id)
        return status


def schedule_memory_distillation(agent_id: int, memory_path: Path, thread_id: int) -> None:
    """(Re)start the inactivity-debounce timer for this agent's memory update."""
    existing = _memory_timers.pop(agent_id, None)
    if existing:
        existing.cancel()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    handle = loop.call_later(
        MEMORY_DEBOUNCE_SECONDS,
        lambda: asyncio.create_task(_run_distillation(agent_id, memory_path)),
    )
    _memory_timers[agent_id] = handle


async def _run_distillation(agent_id: int, memory_path: Path) -> None:
    """Collect messages since the agent's watermark, distil, advance watermark."""
    from sqlalchemy import select
    from app.db.models import Agent as _Agent, AgentRun as _AgentRun

    async with _session_factory() as db:
        agent = await db.get(_Agent, agent_id)
        if agent is None:
            return
        watermark = agent.memory_watermark
        run_threads = (await db.execute(
            select(_AgentRun.thread_id).where(_AgentRun.agent_id == agent_id)
        )).scalars().all()
        if not run_threads:
            return
        msgs = (await db.execute(
            select(Message)
            .where(Message.thread_id.in_(run_threads))
            .where(Message.id > watermark)
            .order_by(Message.id.asc())
        )).scalars().all()
        if not msgs:
            return
        new_text = "\n".join(f"[{m.role}] {m.content}" for m in msgs if m.content)
        max_id = max(m.id for m in msgs)

    await distil_memory(agent_id, memory_path, new_text)

    async with _session_factory() as db:
        agent = await db.get(_Agent, agent_id)
        if agent is not None:
            agent.memory_watermark = max_id
            await db.commit()
    _memory_timers.pop(agent_id, None)
