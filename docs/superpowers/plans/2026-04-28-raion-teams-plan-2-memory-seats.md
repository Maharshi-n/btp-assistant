# RAION Teams — Plan 2: Memory System + Seat Integrations

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the four-tier memory system (ephemeral/important/skill/RAG), the memory promotion pipeline, and all seat integrations (WhatsApp, folder/watchdog, Telegram, webhook, cron poll) so agents can perceive their environment and remember what they learn.

**Architecture:** Memory lives in the `agent_memory` table (created in Plan 1). A `MemoryManager` class handles reads/writes/promotion per agent. Seats are managed by a `SeatManager` that starts watchers (watchdog for folders, polling for WhatsApp fallback) and pushes events into `agent_inbox` via `AgentRuntime.push_event()`. The real tool implementations replace the stubs from Plan 1.

**Tech Stack:** Python 3.11+, SQLAlchemy 2 async, OpenAI embeddings (text-embedding-3-small) for RAG, `watchdog>=4.0.0` for folder watching, numpy for cosine similarity (RAG search), APScheduler for cron poll seats.

**Prerequisite:** Plan 1 must be fully implemented. All DB tables exist. `AgentRuntime` is running. `app/teams/tools.py` has stubs.

**Implement in order. Do not skip tasks. Each task ends with a commit.**

---

## Codebase Context

- `app/teams/models_teams.py` — all Teams models including `AgentMemory`, `AgentSeat`, `AgentInbox`
- `app/teams/runtime.py` — `AgentRuntime` with `push_event()` method. Import and use this.
- `app/teams/tools.py` — stub tools to be replaced with real implementations in Task 5
- `app/db/engine.py` — `AsyncSessionLocal` for all DB access
- `app/config.py` — `OPENAI_API_KEY`, `WORKSPACE_DIR`
- `app/tools/rag.py` — existing RAG implementation in RAION. Reference for embedding approach but do NOT reuse — agent RAG is per-agent isolated.
- Existing automations use APScheduler via `app/automations/runtime.py` — follow same pattern

---

## File Map

**New files:**
- `app/teams/memory.py` — `MemoryManager`: read/write/search/promote all four tiers
- `app/teams/seats.py` — `SeatManager`: start/stop seat watchers, push events to inbox
- `tests/teams/test_memory.py` — memory system tests
- `tests/teams/test_seats.py` — seat integration tests

**Modified files:**
- `app/teams/tools.py` — replace stubs with real implementations for memory tools
- `app/teams/runtime.py` — integrate `MemoryManager` into tick pipeline (context building + storing outputs)
- `app/main.py` — start/stop `SeatManager` in startup/shutdown hooks

---

## Task 1: MemoryManager — Ephemeral + Important + Skill Tiers

**Files:**
- Create: `app/teams/memory.py`

RAG tier (embedding search) comes in Task 2. This task handles the three simpler tiers first.

- [ ] **Step 1: Create `app/teams/memory.py`**

```python
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select, update

from app.db.engine import AsyncSessionLocal
from app.teams.models_teams import AgentMemory

logger = logging.getLogger(__name__)

IMPORTANT_TOKEN_BUDGET = 2000  # default max tokens for important tier


def _count_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token."""
    return max(1, len(text) // 4)


class MemoryManager:
    """Read, write, search, and promote agent memory across all four tiers."""

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def write(
        self,
        agent_id: int,
        tier: str,
        key: str,
        content: str,
        ttl_hours: float = 24.0,
        promoted_from: Optional[str] = None,
    ) -> AgentMemory:
        """Upsert a memory entry. For important tier, enforces token budget."""
        token_count = _count_tokens(content)

        if tier == "important":
            await self._enforce_budget(agent_id, token_count)

        ttl_expires_at = None
        if tier == "ephemeral":
            ttl_expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == tier,
                    AgentMemory.key == key,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.content = content
                existing.token_count = token_count
                existing.ttl_expires_at = ttl_expires_at
                if promoted_from:
                    existing.promoted_from_tier = promoted_from
                await db.commit()
                await db.refresh(existing)
                return existing
            else:
                mem = AgentMemory(
                    agent_id=agent_id,
                    tier=tier,
                    key=key,
                    content=content,
                    token_count=token_count,
                    ttl_expires_at=ttl_expires_at,
                    promoted_from_tier=promoted_from,
                )
                db.add(mem)
                await db.commit()
                await db.refresh(mem)
                return mem

    async def _enforce_budget(self, agent_id: int, new_tokens: int) -> None:
        """If adding new_tokens would exceed budget, demote oldest low-access entries."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == "important",
                ).order_by(AgentMemory.access_count.asc(), AgentMemory.last_accessed_at.asc())
            )
            entries = result.scalars().all()
            current_tokens = sum(e.token_count for e in entries)

            while current_tokens + new_tokens > IMPORTANT_TOKEN_BUDGET and entries:
                oldest = entries.pop(0)
                logger.info(
                    "Budget: demoting important memory key=%s for agent %d", oldest.key, agent_id
                )
                oldest.tier = "rag"
                oldest.promoted_from_tier = "important"
                current_tokens -= oldest.token_count
            await db.commit()

    # ------------------------------------------------------------------
    # Read / load for context injection
    # ------------------------------------------------------------------

    async def load_important(self, agent_id: int) -> list[AgentMemory]:
        return await self._load_tier(agent_id, "important")

    async def load_ephemeral(self, agent_id: int) -> list[AgentMemory]:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == "ephemeral",
                ).where(
                    (AgentMemory.ttl_expires_at.is_(None)) |
                    (AgentMemory.ttl_expires_at > now)
                )
            )
            entries = result.scalars().all()
        await self._bump_access(agent_id, [e.id for e in entries])
        return entries

    async def load_skills_index(self, agent_id: int) -> list[AgentMemory]:
        return await self._load_tier(agent_id, "skill")

    async def _load_tier(self, agent_id: int, tier: str) -> list[AgentMemory]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == tier,
                )
            )
            entries = result.scalars().all()
        await self._bump_access(agent_id, [e.id for e in entries])
        return entries

    async def _bump_access(self, agent_id: int, ids: list[int]) -> None:
        if not ids:
            return
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            for mid in ids:
                await db.execute(
                    update(AgentMemory)
                    .where(AgentMemory.id == mid)
                    .values(
                        access_count=AgentMemory.access_count + 1,
                        last_accessed_at=now,
                    )
                )
            await db.commit()

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, agent_id: int, key: str, tier: str) -> bool:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                delete(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == tier,
                    AgentMemory.key == key,
                ).returning(AgentMemory.id)
            )
            deleted = result.fetchone()
            await db.commit()
        return deleted is not None

    # ------------------------------------------------------------------
    # Promotion
    # ------------------------------------------------------------------

    async def promote(self, agent_id: int, key: str, from_tier: str, target_tier: str) -> bool:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == from_tier,
                    AgentMemory.key == key,
                )
            )
            entry = result.scalar_one_or_none()
            if not entry:
                return False

            if target_tier == "important":
                await self._enforce_budget(agent_id, entry.token_count)

            entry.tier = target_tier
            entry.promoted_from_tier = from_tier
            entry.ttl_expires_at = None  # promoted entries don't expire
            await db.commit()
        return True

    # ------------------------------------------------------------------
    # Cleanup (run hourly by promotion pipeline)
    # ------------------------------------------------------------------

    async def cleanup_expired_ephemeral(self, agent_id: int) -> int:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                delete(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == "ephemeral",
                    AgentMemory.ttl_expires_at <= now,
                ).returning(AgentMemory.id)
            )
            deleted = len(result.fetchall())
            await db.commit()
        return deleted

    async def run_promotion_pipeline(self, agent_id: int) -> dict:
        """
        Hourly background task:
        - Promote RAG entries accessed 5+ times in 7 days → skill
        - Promote RAG entries accessed 10+ times in 7 days (factual) → important (pending)
        - Demote important entries not accessed in 30 days → RAG
        - Delete expired ephemeral entries
        """
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
        cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
        stats = {"promoted_to_skill": 0, "demoted_to_rag": 0, "ephemeral_deleted": 0}

        async with AsyncSessionLocal() as db:
            # RAG → Skill (accessed 5+ in 7 days)
            result = await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == "rag",
                    AgentMemory.access_count >= 5,
                    AgentMemory.last_accessed_at >= cutoff_7d,
                )
            )
            for entry in result.scalars().all():
                entry.tier = "skill"
                entry.promoted_from_tier = "rag"
                entry.ttl_expires_at = None
                stats["promoted_to_skill"] += 1

            # Important → RAG (not accessed in 30 days)
            result = await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_id,
                    AgentMemory.tier == "important",
                ).where(
                    (AgentMemory.last_accessed_at.is_(None)) |
                    (AgentMemory.last_accessed_at < cutoff_30d)
                )
            )
            for entry in result.scalars().all():
                entry.tier = "rag"
                entry.promoted_from_tier = "important"
                stats["demoted_to_rag"] += 1

            await db.commit()

        stats["ephemeral_deleted"] = await self.cleanup_expired_ephemeral(agent_id)
        return stats

    # ------------------------------------------------------------------
    # Context block builders (for system prompt injection)
    # ------------------------------------------------------------------

    def format_important_block(self, entries: list[AgentMemory]) -> str:
        if not entries:
            return ""
        lines = "\n".join(f"- [{e.key}] {e.content}" for e in entries)
        return f"\n\n━━━ IMPORTANT MEMORY ━━━\n{lines}"

    def format_ephemeral_block(self, entries: list[AgentMemory]) -> str:
        if not entries:
            return ""
        lines = "\n".join(f"- {e.content}" for e in entries)
        return f"\n\n━━━ RECENT NOTES ━━━\n{lines}"

    def format_skills_block(self, entries: list[AgentMemory]) -> str:
        if not entries:
            return ""
        lines = "\n".join(f'- "{e.key}": {e.content}' for e in entries)
        return f"\n\n━━━ MY SKILLS ━━━\nCall read_skill(name) when relevant:\n{lines}"
```

- [ ] **Step 2: Create `tests/teams/test_memory.py`**

```python
from __future__ import annotations

import pytest
from app.teams.memory import MemoryManager, IMPORTANT_TOKEN_BUDGET
from app.teams.models_teams import Agent
from app.db.engine import AsyncSessionLocal
from sqlalchemy import select


@pytest.fixture
async def agent():
    async with AsyncSessionLocal() as db:
        a = Agent(name="Memory Test", slug="mem-test-002", persona="test", status="active")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        yield a
        await db.delete(a)
        await db.commit()


@pytest.mark.asyncio
async def test_write_and_load_important(agent):
    mm = MemoryManager()
    await mm.write(agent.id, "important", "deadline", "Friday 3pm")
    entries = await mm.load_important(agent.id)
    assert any(e.key == "deadline" and "Friday" in e.content for e in entries)


@pytest.mark.asyncio
async def test_write_upserts_existing(agent):
    mm = MemoryManager()
    await mm.write(agent.id, "important", "rule", "be formal")
    await mm.write(agent.id, "important", "rule", "be very formal")
    entries = await mm.load_important(agent.id)
    rule_entries = [e for e in entries if e.key == "rule"]
    assert len(rule_entries) == 1
    assert rule_entries[0].content == "be very formal"


@pytest.mark.asyncio
async def test_ephemeral_ttl_cleanup(agent):
    from datetime import timedelta, timezone, datetime
    mm = MemoryManager()
    await mm.write(agent.id, "ephemeral", "note", "temp note", ttl_hours=0.000001)
    import asyncio; await asyncio.sleep(0.01)  # let TTL expire
    deleted = await mm.cleanup_expired_ephemeral(agent.id)
    assert deleted >= 1


@pytest.mark.asyncio
async def test_promote_rag_to_skill(agent):
    mm = MemoryManager()
    await mm.write(agent.id, "rag", "pattern", "Raj is always late on Fridays")
    success = await mm.promote(agent.id, "pattern", "rag", "skill")
    assert success is True
    skills = await mm.load_skills_index(agent.id)
    assert any(e.key == "pattern" for e in skills)


@pytest.mark.asyncio
async def test_delete_memory(agent):
    mm = MemoryManager()
    await mm.write(agent.id, "ephemeral", "deleteme", "to be deleted")
    deleted = await mm.delete(agent.id, "deleteme", "ephemeral")
    assert deleted is True
    entries = await mm.load_ephemeral(agent.id)
    assert not any(e.key == "deleteme" for e in entries)


@pytest.mark.asyncio
async def test_important_budget_enforcement(agent):
    mm = MemoryManager()
    # Fill budget with large entries
    big_content = "x" * (IMPORTANT_TOKEN_BUDGET * 4)  # way over budget
    await mm.write(agent.id, "important", "big", big_content)
    # Adding another entry should demote the first
    await mm.write(agent.id, "important", "small", "small fact")
    entries = await mm.load_important(agent.id)
    total_tokens = sum(e.token_count for e in entries)
    assert total_tokens <= IMPORTANT_TOKEN_BUDGET + 50  # small tolerance


@pytest.mark.asyncio
async def test_format_important_block(agent):
    mm = MemoryManager()
    await mm.write(agent.id, "important", "key1", "value one")
    entries = await mm.load_important(agent.id)
    block = mm.format_important_block(entries)
    assert "IMPORTANT MEMORY" in block
    assert "value one" in block
```

- [ ] **Step 3: Run memory tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_memory.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/teams/memory.py tests/teams/test_memory.py
git commit -m "feat: MemoryManager — ephemeral, important, skill tiers with budget enforcement and promotion pipeline"
```

---

## Task 2: RAG Tier — Embeddings + Semantic Search

**Files:**
- Modify: `app/teams/memory.py`

- [ ] **Step 1: Add embedding + RAG search methods to `MemoryManager`**

Add these imports at the top of `app/teams/memory.py`:

```python
import json
import math
from openai import AsyncOpenAI
import app.config as app_config
```

Add these methods to `MemoryManager`:

```python
async def _embed(self, text: str) -> list[float]:
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)
    response = await client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000],  # truncate to avoid token limit
    )
    return response.data[0].embedding

async def write_rag(self, agent_id: int, key: str, content: str) -> AgentMemory:
    """Write a RAG memory entry with embedding."""
    embedding = await self._embed(content)
    embedding_json = json.dumps(embedding)
    token_count = _count_tokens(content)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentMemory).where(
                AgentMemory.agent_id == agent_id,
                AgentMemory.tier == "rag",
                AgentMemory.key == key,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.content = content
            existing.embedding_json = embedding_json
            existing.token_count = token_count
            await db.commit()
            await db.refresh(existing)
            return existing
        else:
            mem = AgentMemory(
                agent_id=agent_id,
                tier="rag",
                key=key,
                content=content,
                embedding_json=embedding_json,
                token_count=token_count,
            )
            db.add(mem)
            await db.commit()
            await db.refresh(mem)
            return mem

async def search_rag(self, agent_id: int, query: str, top_k: int = 5) -> list[AgentMemory]:
    """Semantic search over RAG tier. Returns top_k most similar entries."""
    query_embedding = await self._embed(query)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentMemory).where(
                AgentMemory.agent_id == agent_id,
                AgentMemory.tier == "rag",
                AgentMemory.embedding_json.is_not(None),
            )
        )
        entries = result.scalars().all()

    if not entries:
        return []

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    scored = []
    for entry in entries:
        emb = json.loads(entry.embedding_json)
        score = cosine(query_embedding, emb)
        scored.append((score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [e for _, e in scored[:top_k]]
    await self._bump_access(agent_id, [e.id for e in top])
    return top

async def prune_rag(self, agent_id: int, max_chunks: int = 10000) -> int:
    """Remove oldest + least-accessed RAG chunks when over limit."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentMemory).where(
                AgentMemory.agent_id == agent_id,
                AgentMemory.tier == "rag",
            ).order_by(
                AgentMemory.access_count.asc(),
                AgentMemory.last_accessed_at.asc(),
            )
        )
        entries = result.scalars().all()

    if len(entries) <= max_chunks:
        return 0

    to_delete = entries[:len(entries) - max_chunks]
    async with AsyncSessionLocal() as db:
        for e in to_delete:
            await db.delete(e)
        await db.commit()
    return len(to_delete)

def format_rag_block(self, entries: list[AgentMemory]) -> str:
    if not entries:
        return ""
    lines = "\n".join(f"- {e.content}" for e in entries)
    return f"\n\n━━━ RELEVANT MEMORY ━━━\n{lines}"
```

- [ ] **Step 2: Write RAG tests**

Add to `tests/teams/test_memory.py`:

```python
@pytest.mark.asyncio
async def test_write_and_search_rag(agent):
    mm = MemoryManager()
    await mm.write_rag(agent.id, "staff-note-1", "Raj submitted his campaign report at 4pm")
    await mm.write_rag(agent.id, "staff-note-2", "Priya sent the invoice to Finance")
    await mm.write_rag(agent.id, "staff-note-3", "Team lunch was scheduled for Friday")

    results = await mm.search_rag(agent.id, "who submitted the campaign report?", top_k=1)
    assert len(results) == 1
    assert "Raj" in results[0].content


@pytest.mark.asyncio
async def test_rag_prune(agent):
    mm = MemoryManager()
    for i in range(5):
        await mm.write_rag(agent.id, f"chunk-{i}", f"content number {i}")

    pruned = await mm.prune_rag(agent.id, max_chunks=3)
    assert pruned == 2
```

- [ ] **Step 3: Run all memory tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_memory.py -v
```

Expected: all 9 tests PASS. (RAG tests make real OpenAI API calls — ensure `OPENAI_API_KEY` is set in `.env`.)

- [ ] **Step 4: Commit**

```bash
git add app/teams/memory.py tests/teams/test_memory.py
git commit -m "feat: RAG tier — OpenAI embeddings, cosine similarity search, pruning"
```

---

## Task 3: Wire Memory into Tick Pipeline

**Files:**
- Modify: `app/teams/runtime.py`

Update `_run_tick` and `_build_system_prompt` to load and inject memory into every agent tick.

- [ ] **Step 1: Add `MemoryManager` import to `app/teams/runtime.py`**

```python
from app.teams.memory import MemoryManager
```

Add instance to `AgentRuntime.__init__`:
```python
self._memory = MemoryManager()
```

- [ ] **Step 2: Update `_run_tick` to build memory context and store outputs**

Replace the existing `_run_tick` method with:

```python
async def _run_tick(self, agent_id: int) -> tuple[bool, int]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()

    if not agent or agent.status != "active":
        return False, 0

    # Step 1: Drain inbox
    inbox_items = await self._drain_inbox(agent_id)
    if not inbox_items:
        return False, 0

    # Step 2: Build memory context
    important = await self._memory.load_important(agent_id)
    ephemeral = await self._memory.load_ephemeral(agent_id)
    skills = await self._memory.load_skills_index(agent_id)

    event_text = self._format_inbox_items(inbox_items)
    rag_results = await self._memory.search_rag(agent_id, event_text, top_k=5)

    memory_blocks = (
        self._memory.format_important_block(important)
        + self._memory.format_ephemeral_block(ephemeral)
        + self._memory.format_skills_block(skills)
        + self._memory.format_rag_block(rag_results)
    )

    # Step 3: Build system prompt with memory injected
    system_prompt = self._build_system_prompt(agent) + memory_blocks

    # Step 4: Invoke LLM
    actions_taken = await self._invoke_llm(agent, system_prompt, event_text)

    # Step 5: Store raw events to RAG
    for item in inbox_items:
        import hashlib
        key = f"event_{item.id}_{hashlib.md5(item.payload_json.encode()).hexdigest()[:8]}"
        await self._memory.write_rag(agent_id, key, f"[{item.source_type}] {item.payload_json}")

    # Step 6: Mark inbox done
    await self._mark_inbox_done(agent_id, [item.id for item in inbox_items])

    return True, actions_taken
```

- [ ] **Step 3: Run full test suite**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/ -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/teams/runtime.py
git commit -m "feat: inject 4-tier memory into agent tick pipeline — important, ephemeral, skill, RAG"
```

---

## Task 4: Real Memory Tool Implementations

**Files:**
- Modify: `app/teams/tools.py`

Replace the stubs for `write_memory`, `delete_memory`, `promote_memory` with real implementations that call `MemoryManager`.

- [ ] **Step 1: Add a context variable to carry `agent_id` into tools**

Tools need to know which agent is calling them. Add a context variable at the top of `app/teams/tools.py`:

```python
from __future__ import annotations

import contextvars
from langchain_core.tools import tool
from app.teams.memory import MemoryManager

# Set this before invoking LLM in _invoke_llm
current_agent_id: contextvars.ContextVar[int] = contextvars.ContextVar("current_agent_id", default=0)
```

- [ ] **Step 2: Update `_invoke_llm` in `runtime.py` to set the context var**

In `_invoke_llm`, before the LLM loop, add:

```python
from app.teams.tools import current_agent_id
current_agent_id.set(agent.id)
```

- [ ] **Step 3: Replace memory tool stubs in `app/teams/tools.py`**

```python
@tool
def write_memory(tier: str, key: str, content: str, ttl_hours: float = 24.0) -> str:
    """Write a memory entry to the agent's memory store.
    tier: ephemeral | important | skill | rag
    key: unique identifier for this memory entry
    content: the text to store
    ttl_hours: hours until auto-deletion (ephemeral tier only, ignored for other tiers)
    """
    import asyncio
    agent_id = current_agent_id.get()
    if not agent_id:
        return "Error: no agent context"
    mm = MemoryManager()
    if tier == "rag":
        asyncio.get_event_loop().run_until_complete(mm.write_rag(agent_id, key, content))
    else:
        asyncio.get_event_loop().run_until_complete(
            mm.write(agent_id, tier, key, content, ttl_hours=ttl_hours)
        )
    return f"Memory written: [{tier}] {key}"


@tool
def delete_memory(key: str, tier: str = "ephemeral") -> str:
    """Delete a memory entry by key and tier."""
    import asyncio
    agent_id = current_agent_id.get()
    if not agent_id:
        return "Error: no agent context"
    mm = MemoryManager()
    deleted = asyncio.get_event_loop().run_until_complete(mm.delete(agent_id, key, tier))
    return f"Memory deleted: {key} (found={deleted})"


@tool
def promote_memory(key: str, from_tier: str, target_tier: str) -> str:
    """Promote a memory entry from one tier to a higher tier.
    from_tier: current tier (rag | ephemeral | skill)
    target_tier: destination tier (skill | important)
    """
    import asyncio
    agent_id = current_agent_id.get()
    if not agent_id:
        return "Error: no agent context"
    mm = MemoryManager()
    success = asyncio.get_event_loop().run_until_complete(
        mm.promote(agent_id, key, from_tier, target_tier)
    )
    if success:
        return f"Promoted [{from_tier}] {key} → [{target_tier}]"
    return f"Not found: [{from_tier}] {key}"
```

- [ ] **Step 4: Commit**

```bash
git add app/teams/tools.py app/teams/runtime.py
git commit -m "feat: implement memory tools — write_memory, delete_memory, promote_memory now call MemoryManager"
```

---

## Task 5: SeatManager — WhatsApp + Telegram + Webhook Seats

**Files:**
- Create: `app/teams/seats.py`

- [ ] **Step 1: Create `app/teams/seats.py`**

```python
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select

from app.db.engine import AsyncSessionLocal
from app.teams.models_teams import Agent, AgentSeat

logger = logging.getLogger(__name__)


class SeatManager:
    """Manages active seat watchers for all agents. Pushes events into agent_inbox."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime  # AgentRuntime instance
        self._folder_observers: dict[int, Any] = {}  # seat_id → watchdog Observer

    async def start(self) -> None:
        await self._start_all_seats()
        logger.info("SeatManager started.")

    async def stop(self) -> None:
        for seat_id, observer in list(self._folder_observers.items()):
            try:
                observer.stop()
                observer.join(timeout=3)
            except Exception:
                pass
        self._folder_observers.clear()
        logger.info("SeatManager stopped.")

    async def _start_all_seats(self) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentSeat).where(AgentSeat.enabled == True)
            )
            seats = result.scalars().all()

        for seat in seats:
            await self.start_seat(seat.id)

    async def start_seat(self, seat_id: int) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AgentSeat).where(AgentSeat.id == seat_id))
            seat = result.scalar_one_or_none()
            if not seat or not seat.enabled:
                return

        if seat.seat_type == "folder":
            await self._start_folder_watcher(seat)
        elif seat.seat_type in ("whatsapp_group", "telegram_chat", "webhook", "cron_poll"):
            # These are push-based (webhooks) or handled by RAION's existing webhook routes
            # Folder is the only one needing an active watcher process
            logger.debug("Seat %d type=%s: push-based, no watcher needed", seat_id, seat.seat_type)

    async def stop_seat(self, seat_id: int) -> None:
        if seat_id in self._folder_observers:
            observer = self._folder_observers.pop(seat_id)
            observer.stop()
            observer.join(timeout=3)

    async def _start_folder_watcher(self, seat: AgentSeat) -> None:
        """Start a watchdog observer for a folder seat."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            config = seat.config
            folder_path = config.get("folder_path", "")
            if not folder_path:
                logger.warning("Folder seat %d has no folder_path configured", seat.id)
                return

            agent_id = seat.agent_id
            runtime = self._runtime
            recursive = config.get("watch_recursive", False)
            extensions = config.get("file_extensions_filter", [])
            ignore_patterns = config.get("ignore_patterns", [])

            class Handler(FileSystemEventHandler):
                def on_created(self, event):
                    if event.is_directory:
                        return
                    path = event.src_path
                    if extensions and not any(path.endswith(ext) for ext in extensions):
                        return
                    if any(pat in path for pat in ignore_patterns):
                        return
                    payload = {"event": "created", "path": path, "seat_id": seat.id}
                    asyncio.run_coroutine_threadsafe(
                        runtime.push_event(agent_id, "folder", payload, priority="normal"),
                        asyncio.get_event_loop(),
                    )

                def on_modified(self, event):
                    if event.is_directory:
                        return
                    path = event.src_path
                    if extensions and not any(path.endswith(ext) for ext in extensions):
                        return
                    payload = {"event": "modified", "path": path, "seat_id": seat.id}
                    asyncio.run_coroutine_threadsafe(
                        runtime.push_event(agent_id, "folder", payload, priority="normal"),
                        asyncio.get_event_loop(),
                    )

            observer = Observer()
            observer.schedule(Handler(), folder_path, recursive=recursive)
            observer.start()
            self._folder_observers[seat.id] = observer
            logger.info("Folder watcher started: agent=%d path=%s", agent_id, folder_path)

        except Exception as exc:
            logger.error("Failed to start folder watcher for seat %d: %s", seat.id, exc)

    # ------------------------------------------------------------------
    # Event push helpers (called by webhook routes)
    # ------------------------------------------------------------------

    async def push_whatsapp_event(self, group_id: str, sender: str,
                                   message: str, message_type: str = "text") -> None:
        """Called by /webhook/whatsapp when a message arrives for a watched group."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentSeat).where(
                    AgentSeat.seat_type == "whatsapp_group",
                    AgentSeat.enabled == True,
                )
            )
            seats = result.scalars().all()

        for seat in seats:
            config = seat.config
            if config.get("group_id") == group_id:
                payload = {
                    "group_id": group_id,
                    "sender": sender,
                    "message": message,
                    "message_type": message_type,
                    "seat_id": seat.id,
                }
                await self._runtime.push_event(
                    seat.agent_id, "whatsapp_group", payload, priority="normal"
                )

    async def push_telegram_event(self, chat_id: str, sender: str, message: str) -> None:
        """Called by /webhook/telegram when a message arrives for a watched chat."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentSeat).where(
                    AgentSeat.seat_type == "telegram_chat",
                    AgentSeat.enabled == True,
                )
            )
            seats = result.scalars().all()

        for seat in seats:
            config = seat.config
            if str(config.get("chat_id", "")) == str(chat_id):
                payload = {"chat_id": chat_id, "sender": sender,
                           "message": message, "seat_id": seat.id}
                await self._runtime.push_event(
                    seat.agent_id, "telegram_chat", payload, priority="normal"
                )

    async def push_webhook_event(self, agent_slug: str, payload: dict) -> bool:
        """Called by /webhook/agent/<slug>. Returns True if agent found."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Agent).where(Agent.slug == agent_slug, Agent.status == "active")
            )
            agent = result.scalar_one_or_none()

        if not agent:
            return False

        await self._runtime.push_event(agent.id, "webhook", payload, priority="normal")
        return True


# Module-level singleton
_seat_manager: SeatManager | None = None


def get_seat_manager() -> SeatManager:
    if _seat_manager is None:
        raise RuntimeError("SeatManager not initialised")
    return _seat_manager


async def init_seat_manager(runtime: Any) -> None:
    global _seat_manager
    _seat_manager = SeatManager(runtime)
    await _seat_manager.start()


async def shutdown_seat_manager() -> None:
    global _seat_manager
    if _seat_manager:
        await _seat_manager.stop()
    _seat_manager = None
```

- [ ] **Step 2: Wire SeatManager into `app/main.py`**

```python
# Add imports:
from app.teams.seats import init_seat_manager, shutdown_seat_manager
from app.teams.runtime import get_runtime

# In startup hook (after init_runtime):
await init_seat_manager(get_runtime())

# In shutdown hook (before shutdown_runtime):
await shutdown_seat_manager()
```

- [ ] **Step 3: Add webhook route for agent seats**

Create `app/web/routes/teams_webhook.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Header
from typing import Optional

from app.teams.seats import get_seat_manager

router = APIRouter()


@router.post("/webhook/agent/{agent_slug}")
async def agent_webhook(
    agent_slug: str,
    request: Request,
    x_agent_token: Optional[str] = Header(None),
):
    """Webhook endpoint for agent webhook seats. POST any JSON payload here."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    sm = get_seat_manager()
    found = await sm.push_webhook_event(agent_slug, payload)
    if not found:
        raise HTTPException(status_code=404, detail=f"No active agent with slug '{agent_slug}'")
    return {"status": "queued"}
```

Register in `app/main.py`:
```python
from app.web.routes.teams_webhook import router as teams_webhook_router
app.include_router(teams_webhook_router)
```

- [ ] **Step 4: Write seat tests**

Create `tests/teams/test_seats.py`:

```python
from __future__ import annotations

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from app.teams.seats import SeatManager
from app.teams.models_teams import Agent, AgentSeat, AgentInbox
from app.db.engine import AsyncSessionLocal
from sqlalchemy import select


@pytest.fixture
async def agent_with_whatsapp_seat():
    async with AsyncSessionLocal() as db:
        agent = Agent(name="WA Seat Test", slug="wa-seat-test-001",
                      persona="test", status="active")
        db.add(agent)
        await db.flush()
        seat = AgentSeat(
            agent_id=agent.id,
            seat_type="whatsapp_group",
            config_json=json.dumps({"group_id": "120363000000001@g.us"}),
            enabled=True,
        )
        db.add(seat)
        await db.commit()
        await db.refresh(agent)
        yield agent
        await db.delete(agent)
        await db.commit()


@pytest.mark.asyncio
async def test_push_whatsapp_event_routes_to_agent(agent_with_whatsapp_seat):
    mock_runtime = MagicMock()
    mock_runtime.push_event = AsyncMock()
    sm = SeatManager(mock_runtime)

    await sm.push_whatsapp_event(
        group_id="120363000000001@g.us",
        sender="Raj",
        message="Campaign done!",
    )

    mock_runtime.push_event.assert_called_once()
    call_args = mock_runtime.push_event.call_args
    assert call_args.kwargs["source_type"] == "whatsapp_group" or call_args.args[1] == "whatsapp_group"
    payload = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("payload", {})
    assert payload["sender"] == "Raj"


@pytest.mark.asyncio
async def test_push_webhook_event_routes_to_agent(agent_with_whatsapp_seat):
    mock_runtime = MagicMock()
    mock_runtime.push_event = AsyncMock()
    sm = SeatManager(mock_runtime)

    found = await sm.push_webhook_event("wa-seat-test-001", {"data": "test"})
    assert found is True
    mock_runtime.push_event.assert_called_once()


@pytest.mark.asyncio
async def test_push_webhook_event_unknown_agent():
    mock_runtime = MagicMock()
    sm = SeatManager(mock_runtime)
    found = await sm.push_webhook_event("nonexistent-agent", {})
    assert found is False
```

- [ ] **Step 5: Run seat tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_seats.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/teams/seats.py app/web/routes/teams_webhook.py app/main.py tests/teams/test_seats.py
git commit -m "feat: SeatManager — folder watchdog, WhatsApp/Telegram/webhook seat event routing"
```

---

## Task 6: Cron Poll Seat + Promotion Pipeline Scheduler

**Files:**
- Modify: `app/teams/runtime.py`
- Modify: `app/teams/seats.py`

- [ ] **Step 1: Add cron poll seat handling to SeatManager**

Add this method to `SeatManager` in `app/teams/seats.py`:

```python
async def start_cron_poll_seat(self, seat: AgentSeat, scheduler: Any) -> None:
    """Register a cron poll seat as an APScheduler job."""
    config = seat.config
    cron_expr = config.get("cron_expr", "*/15 * * * *")
    fetch_target = config.get("fetch_target", "")
    agent_id = seat.agent_id
    seat_id = seat.id
    runtime = self._runtime

    parts = cron_expr.strip().split()
    if len(parts) != 5:
        logger.warning("Invalid cron_expr for seat %d: %s", seat_id, cron_expr)
        return

    minute, hour, day, month, day_of_week = parts

    async def _poll():
        payload = {
            "seat_id": seat_id,
            "fetch_target": fetch_target,
            "event": "cron_poll",
        }
        await runtime.push_event(agent_id, "cron_poll", payload, priority="normal")

    job_id = f"seat_poll_{seat_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        _poll,
        "cron",
        minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
        id=job_id,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Cron poll seat %d scheduled: %s", seat_id, cron_expr)
```

- [ ] **Step 2: Add hourly promotion pipeline to AgentRuntime**

In `app/teams/runtime.py`, add to the `start` method after `_register_all_active_agents`:

```python
# Schedule hourly memory promotion pipeline
self._scheduler.add_job(
    self._run_promotion_pipeline_all,
    "interval",
    hours=1,
    id="memory_promotion_pipeline",
    max_instances=1,
)
```

Add the method to `AgentRuntime`:

```python
async def _run_promotion_pipeline_all(self) -> None:
    """Run memory promotion pipeline for all active agents."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent).where(Agent.status == "active", Agent.host_node_id.is_(None))
        )
        agents = result.scalars().all()

    for agent in agents:
        try:
            stats = await self._memory.run_promotion_pipeline(agent.id)
            if any(v > 0 for v in stats.values()):
                logger.info("Promotion pipeline agent %d: %s", agent.id, stats)
        except Exception as exc:
            logger.warning("Promotion pipeline failed for agent %d: %s", agent.id, exc)
```

- [ ] **Step 3: Run full test suite**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/ -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/teams/runtime.py app/teams/seats.py
git commit -m "feat: cron poll seat scheduling + hourly memory promotion pipeline"
```

---

## Done — Plan 2 Complete

At this point you have:
- Full 4-tier memory system with RAG embeddings, budget enforcement, and promotion pipeline
- Memory injected into every agent tick (important + ephemeral + skills + RAG search)
- Folder watchdog, WhatsApp, Telegram, webhook, and cron poll seats routing events to agent inbox
- Real `write_memory`, `delete_memory`, `promote_memory` tools
- Hourly background promotion pipeline running

**Next: Plan 3 — Inter-Agent Communication + Environments + Council**
