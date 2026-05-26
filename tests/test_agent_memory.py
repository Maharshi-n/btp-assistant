import asyncio
from pathlib import Path

import pytest

from app.agents import agent_memory


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "1_memory.md"
    agent_memory._atomic_write(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"


def test_atomic_write_overwrites(tmp_path):
    target = tmp_path / "1_memory.md"
    agent_memory._atomic_write(target, "first")
    agent_memory._atomic_write(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_load_memory_missing_returns_empty(tmp_path):
    assert agent_memory.load_memory_file(tmp_path / "nope.md") == ""


def test_load_memory_reads_content(tmp_path):
    target = tmp_path / "m.md"
    target.write_text("## Durable facts\n- x", encoding="utf-8")
    assert agent_memory.load_memory_file(target) == "## Durable facts\n- x"


async def test_per_agent_lock_serializes(tmp_path):
    order = []

    async def worker(n):
        async with agent_memory.agent_lock(99):
            order.append(("start", n))
            await asyncio.sleep(0.01)
            order.append(("end", n))

    await asyncio.gather(worker(1), worker(2))
    assert order in (
        [("start", 1), ("end", 1), ("start", 2), ("end", 2)],
        [("start", 2), ("end", 2), ("start", 1), ("end", 1)],
    )


async def test_distil_memory_rewrites_via_llm(tmp_path, monkeypatch):
    target = tmp_path / "7_memory.md"
    target.write_text("## Durable facts\n- old fact", encoding="utf-8")

    async def fake_llm(old_memory: str, new_messages: str, cap_lines: int) -> str:
        assert "old fact" in old_memory
        assert "NEW EVENT" in new_messages
        return "## Durable facts\n- old fact\n- new fact [src: test]"

    monkeypatch.setattr(agent_memory, "_call_memory_llm", fake_llm)

    new = await agent_memory.distil_memory(
        agent_id=7,
        memory_path=target,
        new_messages_text="NEW EVENT happened",
    )
    assert "new fact" in new
    assert target.read_text(encoding="utf-8") == new
