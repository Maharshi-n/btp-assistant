"""Self-scheduling core: parse natural 'when', create/list/delete an agent's OWN
triggers, and enforce loop-safety (chain depth + dedup). Pure async DB logic with
no LangChain — wrapped by app/agents/agent_tools.py.

register/unregister are module-level names so tests can monkeypatch them here.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.agents.agent_parser import validate_triggers
from app.automations.runtime import register_agent_trigger, unregister_agent_trigger
from app.db.models import AgentTrigger

logger = logging.getLogger(__name__)

SELF_WAKE_MAX_DEPTH = 5
SELF_WAKE_DEDUP_WINDOW = timedelta(seconds=60)

_REL_RE = re.compile(r"in\s+(\d+)\s*(second|minute|hour|day)s?", re.IGNORECASE)


def parse_when(when: str, now: datetime | None = None) -> datetime | None:
    """Parse 'in N minutes/hours/days' or an ISO timestamp into an aware UTC
    datetime. Returns None if it can't be parsed."""
    now = now or datetime.now(timezone.utc)
    s = (when or "").strip()
    m = _REL_RE.search(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = {
            "second": timedelta(seconds=n),
            "minute": timedelta(minutes=n),
            "hour": timedelta(hours=n),
            "day": timedelta(days=n),
        }[unit]
        return now + delta
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def schedule_self(db, agent_id: int, when_iso: str, note: str, chain_depth: int) -> str:
    """Create a one-off self_scheduled trigger. Enforces depth cap + dedup."""
    if chain_depth >= SELF_WAKE_MAX_DEPTH:
        return (f"Self-wake chain limit reached (depth {chain_depth}/{SELF_WAKE_MAX_DEPTH}). "
                "Go idle — do not schedule another self-wake this chain.")
    fire_at = parse_when(when_iso)
    if fire_at is None:
        return f"Could not understand the time {when_iso!r}. Use 'in N minutes/hours/days' or an ISO timestamp."

    existing = (await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.agent_id == agent_id,
            AgentTrigger.trigger_type == "self_scheduled",
        )
    )).scalars().all()
    for t in existing:
        cfg = json.loads(t.trigger_config_json)
        if cfg.get("note") == note:
            try:
                other = datetime.fromisoformat(cfg.get("fire_at", ""))
                if other.tzinfo is None:
                    other = other.replace(tzinfo=timezone.utc)
                if abs(other - fire_at) <= SELF_WAKE_DEDUP_WINDOW:
                    return f"Already scheduled a self-wake for {note!r} around that time — skipping duplicate."
            except (ValueError, TypeError):
                continue

    trig = AgentTrigger(
        agent_id=agent_id,
        trigger_type="self_scheduled",
        trigger_config_json=json.dumps({
            "fire_at": fire_at.isoformat(),
            "note": note,
            "chain_depth": chain_depth + 1,
        }),
        created_by="self",
    )
    db.add(trig)
    await db.commit()
    await db.refresh(trig)
    register_agent_trigger(trig, loop=None)
    return f"Scheduled a self-wake at {fire_at.isoformat()} to: {note}."


async def create_trigger(db, agent_id: int, trigger_type: str, config: dict, description: str) -> str:
    """Create a permanent recurring trigger on this agent. Validated."""
    try:
        validate_triggers([{"trigger_type": trigger_type, "trigger_config": config}])
    except ValueError as exc:
        return f"Invalid trigger: {exc}"
    trig = AgentTrigger(
        agent_id=agent_id,
        trigger_type=trigger_type,
        trigger_config_json=json.dumps(config or {}),
        created_by="self",
    )
    db.add(trig)
    await db.commit()
    await db.refresh(trig)
    register_agent_trigger(trig, loop=None)
    return f"Created a {trigger_type} trigger ({description})."


async def list_triggers(db, agent_id: int) -> str:
    """Human-readable list of this agent's own triggers."""
    trigs = (await db.execute(
        select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
    )).scalars().all()
    if not trigs:
        return "You have no triggers."
    lines = []
    for t in trigs:
        by = t.created_by or "user"
        lines.append(f"#{t.id} [{t.trigger_type}] (by {by}) {t.trigger_config_json}")
    return "Your triggers:\n" + "\n".join(lines)


async def delete_trigger(db, agent_id: int, trigger_id: int) -> str:
    """Delete one of THIS agent's triggers. Refuses others'."""
    t = await db.get(AgentTrigger, trigger_id)
    if t is None or t.agent_id != agent_id:
        return "That trigger is not yours (or does not exist) — not deleted."
    unregister_agent_trigger(t.id)
    await db.delete(t)
    await db.commit()
    return f"Deleted trigger #{trigger_id}."
