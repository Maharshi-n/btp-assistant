"""Agent-only LangChain tools: self-scheduling, own-trigger CRUD, and recall.
Bound ONLY on agent runs (see supervisor._tools_for_run). Each resolves the
current agent_id from the run config and may only act on that agent's own data.
"""
from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

import app.config as app_config
from app.agents import agent_episodes, agent_schedule
from app.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _ctx_agent_id(config) -> int | None:
    cfg = config.get("configurable", {}) if isinstance(config, dict) else {}
    aid = cfg.get("agent_id")
    return int(aid) if aid is not None else None


def _ctx_chain_depth(config) -> int:
    cfg = config.get("configurable", {}) if isinstance(config, dict) else {}
    try:
        return int(cfg.get("chain_depth", 0))
    except (TypeError, ValueError):
        return 0


@tool
async def agent_schedule_self(when: str, note: str, config: RunnableConfig = None) -> str:
    """Wake yourself again later. Use for a follow-up, a retry after a delay, or to
    continue a task you can't finish now.

    Args:
        when: When to wake — 'in 30 minutes', 'in 2 hours', 'in 1 day', or an ISO timestamp.
        note: Why you're waking yourself (what to do then). Be specific.
    """
    aid = _ctx_agent_id(config)
    if aid is None:
        return "No agent context — cannot self-schedule."
    depth = _ctx_chain_depth(config)
    async with AsyncSessionLocal() as db:
        return await agent_schedule.schedule_self(db, aid, when_iso=when, note=note, chain_depth=depth)


@tool
async def agent_create_trigger(trigger_type: str, config_json: dict, description: str,
                               config: RunnableConfig = None) -> str:
    """Create a permanent recurring trigger on yourself so you wake on future events.

    Args:
        trigger_type: one of cron, gmail_any_new, gmail_new_from_sender,
            gmail_keyword_match, fs_new_in_folder, whatsapp_group_new, whatsapp_keyword_match.
        config_json: the trigger config, e.g. {"cron": "0 9 * * *"} or {"sender": "x@y.com"}.
        description: short human description of what this trigger is for.
    """
    aid = _ctx_agent_id(config)
    if aid is None:
        return "No agent context — cannot create a trigger."
    async with AsyncSessionLocal() as db:
        return await agent_schedule.create_trigger(db, aid, trigger_type, config_json or {}, description)


@tool
async def agent_list_triggers(config: RunnableConfig = None) -> str:
    """List your own triggers (so you can avoid creating duplicates)."""
    aid = _ctx_agent_id(config)
    if aid is None:
        return "No agent context."
    async with AsyncSessionLocal() as db:
        return await agent_schedule.list_triggers(db, aid)


@tool
async def agent_delete_trigger(trigger_id: int, config: RunnableConfig = None) -> str:
    """Delete one of your own triggers by its id (from agent_list_triggers)."""
    aid = _ctx_agent_id(config)
    if aid is None:
        return "No agent context."
    async with AsyncSessionLocal() as db:
        return await agent_schedule.delete_trigger(db, aid, trigger_id)


@tool
async def agent_recall(query: str, since: str = "", config: RunnableConfig = None) -> str:
    """Search your own past runs (what you saw/did before).

    Args:
        query: keywords to match in past run summaries/actions. Empty = all.
        since: optional ISO date floor, e.g. '2026-05-20'.
    """
    aid = _ctx_agent_id(config)
    if aid is None:
        return "No agent context."
    path = app_config.WORKSPACE_DIR / "agents" / f"{aid}_episodes.jsonl"
    hits = agent_episodes.recall(path, query=query, since=since or None)
    if not hits:
        return "No matching past runs."
    lines = [f"[{h.get('ts','')}] ({h.get('trigger_type','')}) {h.get('summary','')}" for h in hits[-30:]]
    return "Past runs:\n" + "\n".join(lines)


AGENT_ONLY_TOOLS = [
    agent_schedule_self,
    agent_create_trigger,
    agent_list_triggers,
    agent_delete_trigger,
    agent_recall,
]
