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

MANUAL_BEGIN = "<!-- MANUAL_NOTES_BEGIN -->"
MANUAL_END = "<!-- MANUAL_NOTES_END -->"


def split_manual_notes(text: str) -> tuple[str, str]:
    """Return (auto_sections, manual_block_inner). If markers are absent or
    malformed, the whole file is treated as auto and manual is ''."""
    if MANUAL_BEGIN in text and MANUAL_END in text:
        before, _, rest = text.partition(MANUAL_BEGIN)
        inner, _, after = rest.partition(MANUAL_END)
        auto = (before + after).strip()
        return auto, inner.strip()
    return text.strip(), ""


def join_manual_notes(auto: str, manual: str) -> str:
    """Reassemble the file: auto sections, then the protected manual block."""
    return (
        auto.rstrip()
        + "\n\n"
        + MANUAL_BEGIN + "\n"
        + manual.strip()
        + "\n" + MANUAL_END + "\n"
    )

_MEMORY_SYSTEM_PROMPT = (
    "You maintain an AI agent's long-term memory file. You receive the CURRENT "
    "memory and a batch of NEW events. Return the COMPLETE updated memory file.\n"
    "IMPORTANCE FILTER (most important rule): store something ONLY if it is genuinely "
    "important for the agent's future runs — durable facts, decisions, commitments, "
    "people/contacts, open tasks. If the new events contain nothing worth remembering "
    "(small talk, acknowledgements, one-off chatter), return the CURRENT memory UNCHANGED. "
    "Do not pad memory with trivia.\n"
    "SOURCE: the events are labelled with their source. Events from a LIVE CHAT are the "
    "user talking to the agent directly; events from a TRIGGER are autonomous runs. Record "
    "the source when it matters (e.g. 'user said in chat ...').\n"
    "Rules:\n"
    "- Keep these sections, in order: '## Durable facts', '## Open tasks', "
    "'## Recent decisions', '## People/contacts'. Omit a section only if truly empty.\n"
    "- ADD new important facts/tasks/decisions. UPDATE facts that changed. REMOVE "
    "things no longer true. Do NOT just append.\n"
    "- '## Recent decisions' is a rolling window — keep only the most recent ~10.\n"
    "- Do NOT copy raw event logs.\n"
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


async def _call_memory_llm(old_memory: str, new_messages: str, cap_lines: int, source: str = "trigger") -> str:
    """Call gpt-4o-mini to reconcile memory. NO tools are bound — read/write only.

    `source` is "live_chat" (the user talking to the agent) or "trigger" (an
    autonomous run); it's surfaced to the model so it knows how to weigh the events.
    """
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)
    source_label = "LIVE CHAT (user talking to the agent directly)" if source == "live_chat" else "TRIGGER (autonomous run)"
    user = (
        f"EVENT SOURCE: {source_label}\n\n"
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


async def distil_memory(agent_id: int, memory_path: Path, new_messages_text: str, source: str = "trigger") -> str:
    """Reconcile the memory file with new events and write it atomically under lock.

    Returns the new memory content. No-op (returns existing) if new_messages_text
    is blank. `source` is "live_chat" or "trigger" (passed to the memory model so it
    knows the events came from a direct chat vs an autonomous run).
    """
    if not new_messages_text.strip():
        return load_memory_file(memory_path)
    async with agent_lock(agent_id):
        full = load_memory_file(memory_path)
        auto_old, manual = split_manual_notes(full)
        new_auto = await _call_memory_llm(auto_old, new_messages_text, MEMORY_CAP_LINES, source=source)
        if new_auto:
            combined = join_manual_notes(new_auto, manual)
            _atomic_write(memory_path, combined)
            return combined
        return full
