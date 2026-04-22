"""Structured state for multi-round automation conversations.

Each live multi-round flow gets one DB row. Two JSON blobs:

  context_json  — frozen trigger context set once at creation.
                  Any key/value the flow needs to persist (email_from,
                  file_path, calendar_event_id, …). Never modified after
                  the row is created.

  state_json    — evolving state updated round by round.
                  e.g. {"current_draft": "...", "round": 2}

The webhook reads both blobs and injects them verbatim into each
continuation prompt — the LLM never has to copy context forward.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.engine import AsyncSessionLocal
from app.db.models import AutomationConversation


async def create_conversation(
    *,
    automation_id: int | None,
    trigger_kind: str,
    lg_thread_id: str | None = None,
    db_thread_id: int | None = None,
    **context: object,
) -> int:
    """Create a new conversation row and return its id.

    All keyword arguments beyond the named ones are stored as the frozen
    context_json blob (e.g. email_from, email_subject, file_path, ...).
    """
    async with AsyncSessionLocal() as db:
        conv = AutomationConversation(
            automation_id=automation_id,
            trigger_kind=trigger_kind,
            context_json=json.dumps(context),
            state_json="{}",
            lg_thread_id=lg_thread_id,
            db_thread_id=db_thread_id,
            status="active",
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        return conv.id


async def set_lg_thread(conversation_id: int, lg_thread_id: str, db_thread_id: int) -> None:
    """Attach the LangGraph thread ID to a conversation after the first run creates it."""
    async with AsyncSessionLocal() as db:
        conv = await db.get(AutomationConversation, conversation_id)
        if conv:
            conv.lg_thread_id = lg_thread_id
            conv.db_thread_id = db_thread_id
            conv.updated_at = datetime.now(timezone.utc)
            await db.commit()


async def get_conversation(conversation_id: int) -> AutomationConversation | None:
    async with AsyncSessionLocal() as db:
        return await db.get(AutomationConversation, conversation_id)


async def get_context(conversation_id: int) -> dict:
    """Return the frozen context dict for this conversation."""
    conv = await get_conversation(conversation_id)
    if conv is None:
        return {}
    try:
        return json.loads(conv.context_json)
    except Exception:
        return {}


async def get_state(conversation_id: int) -> dict:
    """Return the current mutable state dict for this conversation."""
    conv = await get_conversation(conversation_id)
    if conv is None:
        return {}
    try:
        return json.loads(conv.state_json)
    except Exception:
        return {}


async def update_state(conversation_id: int, **updates: object) -> None:
    """Merge *updates* into state_json (shallow merge — existing keys not in
    *updates* are preserved).

    Example:
        await update_state(conv_id, current_draft="Dear Alice,\\n\\n...")
        await update_state(conv_id, round=2, current_draft="Revised draft...")
    """
    async with AsyncSessionLocal() as db:
        conv = await db.get(AutomationConversation, conversation_id)
        if conv:
            try:
                current = json.loads(conv.state_json)
            except Exception:
                current = {}
            current.update(updates)
            conv.state_json = json.dumps(current)
            conv.updated_at = datetime.now(timezone.utc)
            await db.commit()


async def mark_status(conversation_id: int, status: str) -> None:
    """Set the lifecycle status: 'active' | 'done' | 'cancelled'."""
    async with AsyncSessionLocal() as db:
        conv = await db.get(AutomationConversation, conversation_id)
        if conv:
            conv.status = status
            conv.updated_at = datetime.now(timezone.utc)
            await db.commit()


async def cleanup_old_conversations(hours: int = 48) -> int:
    """Delete conversations older than *hours*. Returns count deleted."""
    from sqlalchemy import delete
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(AutomationConversation).where(
                AutomationConversation.created_at < cutoff
            )
        )
        await db.commit()
        return result.rowcount or 0
