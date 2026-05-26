"""Per-agent memory file: atomic writes under a per-agent lock, plus
LLM-driven distillation (rewrite-not-append) using gpt-4o-mini with no tools."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from pathlib import Path

from openai import AsyncOpenAI

import app.config as app_config

logger = logging.getLogger(__name__)

# agent_id -> asyncio.Lock (serialize all fires/writes for one agent)
_agent_locks: dict[int, asyncio.Lock] = {}

# Cap the memory file size so context stays bounded.
MEMORY_CAP_LINES = 200

_MEMORY_SYSTEM_PROMPT = (
    "You maintain an AI agent's long-term memory file. You receive the CURRENT "
    "memory and a batch of NEW events. Return the COMPLETE updated memory file.\n"
    "Rules:\n"
    "- Keep these sections, in order: '## Durable facts', '## Open tasks', "
    "'## Recent decisions', '## People/contacts'. Omit a section only if truly empty.\n"
    "- ADD new durable facts/tasks/decisions. UPDATE facts that changed. REMOVE "
    "things no longer true. Do NOT just append.\n"
    "- '## Recent decisions' is a rolling window — keep only the most recent ~10.\n"
    "- Record decisions, durable facts, and open tasks. Do NOT copy raw event logs.\n"
    "- Tag any fact derived from external/untrusted input with '[src: ...]'.\n"
    f"- Keep the whole file under {MEMORY_CAP_LINES} lines.\n"
    "Output ONLY the markdown file content — no fences, no commentary."
)


def agent_lock(agent_id: int) -> asyncio.Lock:
    """Return (creating if needed) the lock for one agent. Use as `async with`."""
    lock = _agent_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        _agent_locks[agent_id] = lock
    return lock


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically (temp file in same dir, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_memory_file(path: Path) -> str:
    """Return the memory file's text, or '' if it does not exist."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("load_memory_file failed for %s: %s", path, exc)
        return ""


async def _call_memory_llm(old_memory: str, new_messages: str, cap_lines: int) -> str:
    """Call gpt-4o-mini to reconcile memory. NO tools are bound — read/write only."""
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)
    user = (
        f"CURRENT MEMORY:\n{old_memory or '(empty)'}\n\n"
        f"NEW EVENTS SINCE LAST UPDATE:\n{new_messages}"
    )
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _MEMORY_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    return (resp.choices[0].message.content or "").strip()


async def distil_memory(agent_id: int, memory_path: Path, new_messages_text: str) -> str:
    """Reconcile the memory file with new events and write it atomically under lock.

    Returns the new memory content. No-op (returns existing) if new_messages_text
    is blank.
    """
    if not new_messages_text.strip():
        return load_memory_file(memory_path)
    async with agent_lock(agent_id):
        old = load_memory_file(memory_path)
        new = await _call_memory_llm(old, new_messages_text, MEMORY_CAP_LINES)
        if new:
            _atomic_write(memory_path, new)
            return new
        return old
