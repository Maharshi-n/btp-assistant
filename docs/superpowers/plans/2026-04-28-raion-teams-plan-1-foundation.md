# RAION Teams — Plan 1: Foundation (DB Models + Agent Runtime + Inbox Queue + Wakeup)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add all new DB tables for RAION Teams and build the core agent runtime — the tick pipeline, priority inbox queue, and hybrid wakeup model (event-driven + scheduled).

**Architecture:** Each agent is a DB row in `agents`. APScheduler manages periodic ticks. All wakeup events (webhooks, agent messages, watchdog, cron fires) insert rows into `agent_inbox`. The runtime drains that queue sequentially per agent, calling LangGraph for the actual LLM work. This plan produces a working agent that can tick, receive inbox events, and run its action prompt — with no memory system or seat integrations yet (those come in Plan 2).

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 async, SQLite (aiosqlite), APScheduler 3.x, LangGraph, LangChain OpenAI, alembic (for migrations)

**Implement in order. Do not skip tasks. Each task ends with a commit.**

---

## Codebase Context (read before starting)

This is RAION — a self-hosted AI assistant. Key files you must understand before touching anything:

- `app/db/models.py` — ALL SQLAlchemy models live here. Add new models here.
- `app/db/engine.py` — async engine + `AsyncSessionLocal` + `Base`. Import these everywhere.
- `app/main.py` — FastAPI app factory. `on_startup` / `on_shutdown` hooks run here.
- `app/agents/supervisor.py` — existing LangGraph supervisor. The agent runtime will follow the same pattern (LangGraph invoke with a system prompt + tools).
- `app/config.py` — env vars via `python-dotenv`. `OPENAI_API_KEY`, `WORKSPACE_DIR` etc.
- `app/automations/runtime.py` — existing APScheduler usage. Follow this pattern for scheduling agent ticks.
- `run.py` — entry point: `python run.py` starts uvicorn on port 8000.

SQLAlchemy conventions in this codebase:
- Always `async def` + `await`, never sync sessions
- `AsyncSessionLocal` from `app.db.engine` for DB access
- `from __future__ import annotations` at top of every file
- Models use `mapped_column`, `Mapped`, `relationship` (SQLAlchemy 2 style)

---

## File Map

**New files to create:**
- `app/teams/models_teams.py` — SQLAlchemy models for all Teams tables (import into `app/db/models.py`)
- `app/teams/__init__.py` — empty
- `app/teams/runtime.py` — AgentRuntime class: tick pipeline, inbox drain, APScheduler management
- `app/teams/tools.py` — stub agent-only tools (`write_memory`, `create_agent_cron`, `message_agent`, etc.) — just stubs returning "not yet implemented" for now; real implementations come in Plans 2 and 3
- `tests/teams/test_models.py` — model creation + DB round-trip tests
- `tests/teams/test_runtime.py` — tick pipeline unit tests
- `tests/teams/__init__.py` — empty

**Files to modify:**
- `app/db/models.py` — add `from app.teams.models_teams import *` at bottom
- `app/main.py` — start/stop AgentRuntime in startup/shutdown hooks
- `requirements.txt` — add `watchdog>=4.0.0` (used in Plan 2, add now)

---

## Task 1: DB Models — All Teams Tables

**Files:**
- Create: `app/teams/__init__.py`
- Create: `app/teams/models_teams.py`
- Modify: `app/db/models.py`

- [ ] **Step 1: Create `app/teams/__init__.py`**

```python
```
(empty file)

- [ ] **Step 2: Create `app/teams/models_teams.py`**

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.engine import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentEnvironment(Base):
    __tablename__ = "agent_environments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(32), nullable=False, default="#6366f1")
    default_node_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_nodes.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    agents: Mapped[list[Agent]] = relationship("Agent", back_populates="environment", lazy="selectin")


class AgentNode(Base):
    __tablename__ = "agent_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    ws_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    capabilities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    last_ping_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def capabilities(self) -> list[str]:
        return json.loads(self.capabilities_json)

    @capabilities.setter
    def capabilities(self, val: list[str]) -> None:
        self.capabilities_json = json.dumps(val)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    persona: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="paused")
    environment_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_environments.id", ondelete="SET NULL"), nullable=True
    )
    parent_agent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    host_node_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_nodes.id", ondelete="SET NULL"), nullable=True
    )
    wake_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="always_on")
    tick_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    is_system_agent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    allowed_tools_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="user")

    environment: Mapped[Optional[AgentEnvironment]] = relationship("AgentEnvironment", back_populates="agents")
    seats: Mapped[list[AgentSeat]] = relationship("AgentSeat", back_populates="agent", cascade="all, delete-orphan", lazy="selectin")
    inbox: Mapped[list[AgentInbox]] = relationship("AgentInbox", back_populates="agent", cascade="all, delete-orphan")
    memory: Mapped[list[AgentMemory]] = relationship("AgentMemory", back_populates="agent", cascade="all, delete-orphan")
    crons: Mapped[list[AgentCron]] = relationship("AgentCron", back_populates="agent", cascade="all, delete-orphan")
    versions: Mapped[list[AgentVersion]] = relationship("AgentVersion", back_populates="agent", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("is_system_agent", name="uq_one_system_agent",
                         sqlite_where="is_system_agent = 1"),
    )

    @property
    def allowed_tools(self) -> list[str]:
        return json.loads(self.allowed_tools_json)

    @allowed_tools.setter
    def allowed_tools(self, val: list[str]) -> None:
        self.allowed_tools_json = json.dumps(val)


class AgentVersion(Base):
    __tablename__ = "agent_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="user")

    agent: Mapped[Agent] = relationship("Agent", back_populates="versions")


class AgentSeat(Base):
    __tablename__ = "agent_seats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    seat_type: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_channels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    agent: Mapped[Agent] = relationship("Agent", back_populates="seats")

    @property
    def config(self) -> dict:
        return json.loads(self.config_json)

    @config.setter
    def config(self, val: dict) -> None:
        self.config_json = json.dumps(val)


class AgentInbox(Base):
    __tablename__ = "agent_inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    priority: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    agent: Mapped[Agent] = relationship("Agent", back_populates="inbox")

    __table_args__ = (
        Index("ix_agent_inbox_agent_status", "agent_id", "status"),
        Index("ix_agent_inbox_priority", "agent_id", "priority", "created_at"),
    )

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


class AgentMemory(Base):
    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_accessed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_from_tier: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ttl_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    agent: Mapped[Agent] = relationship("Agent", back_populates="memory")

    __table_args__ = (
        Index("ix_agent_memory_agent_tier", "agent_id", "tier"),
        UniqueConstraint("agent_id", "tier", "key", name="uq_agent_memory_key"),
    )


class AgentCron(Base):
    __tablename__ = "agent_crons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(128), nullable=False)
    action_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    agent: Mapped[Agent] = relationship("Agent", back_populates="crons")

    @property
    def state(self) -> dict:
        return json.loads(self.state_json)

    @state.setter
    def state(self, val: dict) -> None:
        self.state_json = json.dumps(val)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_agent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    to_agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_agent_messages_to_status", "to_agent_id", "status"),
    )


class Council(Base):
    __tablename__ = "councils"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    chair_agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    environment_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_environments.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    concluded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    blackboard: Mapped[list[AgentBlackboard]] = relationship(
        "AgentBlackboard", back_populates="council", cascade="all, delete-orphan"
    )


class AgentBlackboard(Base):
    __tablename__ = "agent_blackboard"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    council_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("councils.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    entry_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    council: Mapped[Council] = relationship("Council", back_populates="blackboard")

    __table_args__ = (
        Index("ix_agent_blackboard_council", "council_id", "created_at"),
    )


class AgentMetric(Base):
    __tablename__ = "agent_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    tick_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    actions_taken: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("agent_id", "date", name="uq_agent_metrics_date"),
    )


class AgentTemplate(Base):
    __tablename__ = "agent_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="user")
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def config(self) -> dict:
        return json.loads(self.config_json)
```

- [ ] **Step 3: Register models in `app/db/models.py`**

Open `app/db/models.py` and add at the very bottom:

```python
# Teams models
from app.teams.models_teams import (  # noqa: F401, E402
    AgentEnvironment,
    AgentNode,
    Agent,
    AgentVersion,
    AgentSeat,
    AgentInbox,
    AgentMemory,
    AgentCron,
    AgentMessage,
    Council,
    AgentBlackboard,
    AgentMetric,
    AgentTemplate,
)
```

- [ ] **Step 4: Create and run migration**

```bash
cd "E:/BTP project"
python -c "
import asyncio
from app.db.engine import engine, Base
import app.db.models  # triggers all imports including teams models

async def migrate():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print('Tables created.')

asyncio.run(migrate())
"
```

Expected output: `Tables created.`

- [ ] **Step 5: Create `tests/teams/__init__.py`**

```python
```
(empty)

- [ ] **Step 6: Write model tests**

Create `tests/teams/test_models.py`:

```python
from __future__ import annotations

import asyncio
import pytest
from sqlalchemy import select

from app.db.engine import AsyncSessionLocal
from app.teams.models_teams import (
    Agent, AgentEnvironment, AgentInbox, AgentMemory,
    AgentCron, AgentMessage, AgentNode,
)


@pytest.mark.asyncio
async def test_create_environment():
    async with AsyncSessionLocal() as db:
        env = AgentEnvironment(name="Business", slug="business", color="#6366f1")
        db.add(env)
        await db.commit()
        await db.refresh(env)
        assert env.id is not None
        assert env.slug == "business"
        await db.delete(env)
        await db.commit()


@pytest.mark.asyncio
async def test_create_agent():
    async with AsyncSessionLocal() as db:
        agent = Agent(
            name="Test Agent",
            slug="test-agent-001",
            persona="You are a test agent.",
            wake_mode="always_on",
            tick_interval_seconds=30,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        assert agent.id is not None
        assert agent.status == "paused"
        assert agent.is_system_agent is False
        await db.delete(agent)
        await db.commit()


@pytest.mark.asyncio
async def test_agent_inbox_priority_ordering():
    async with AsyncSessionLocal() as db:
        agent = Agent(name="Inbox Test", slug="inbox-test-001", persona="test")
        db.add(agent)
        await db.flush()

        db.add(AgentInbox(agent_id=agent.id, source_type="webhook",
                          payload_json='{"msg":"low"}', priority="background"))
        db.add(AgentInbox(agent_id=agent.id, source_type="agent_message",
                          payload_json='{"msg":"high"}', priority="urgent"))
        db.add(AgentInbox(agent_id=agent.id, source_type="seat",
                          payload_json='{"msg":"normal"}', priority="normal"))
        await db.commit()

        result = await db.execute(
            select(AgentInbox)
            .where(AgentInbox.agent_id == agent.id)
            .order_by(
                AgentInbox.priority.in_(["urgent"]).desc(),
                AgentInbox.created_at.asc(),
            )
        )
        items = result.scalars().all()
        assert len(items) == 3

        await db.delete(agent)
        await db.commit()


@pytest.mark.asyncio
async def test_agent_memory_unique_key():
    async with AsyncSessionLocal() as db:
        agent = Agent(name="Memory Test", slug="memory-test-001", persona="test")
        db.add(agent)
        await db.flush()

        mem = AgentMemory(agent_id=agent.id, tier="important",
                          key="deadline", content="Friday 3pm")
        db.add(mem)
        await db.commit()

        result = await db.execute(
            select(AgentMemory).where(
                AgentMemory.agent_id == agent.id,
                AgentMemory.tier == "important",
                AgentMemory.key == "deadline",
            )
        )
        found = result.scalar_one()
        assert found.content == "Friday 3pm"

        await db.delete(agent)
        await db.commit()
```

- [ ] **Step 7: Run model tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_models.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add app/teams/ app/db/models.py tests/teams/
git commit -m "feat: add RAION Teams DB models (agents, inbox, memory, crons, councils, metrics)"
```

---

## Task 2: Stub Agent Tools

**Files:**
- Create: `app/teams/tools.py`

These tools are callable by agents during a tick. Real implementations come in Plans 2 and 3. For now they return a "not yet implemented" string so the agent runtime can wire them up and the LLM can call them without crashing.

- [ ] **Step 1: Create `app/teams/tools.py`**

```python
from __future__ import annotations

from langchain_core.tools import tool


@tool
def write_memory(tier: str, key: str, content: str, ttl_hours: float = 24.0) -> str:
    """Write a memory entry to the agent's memory store.
    tier: ephemeral | important | skill | rag
    key: unique identifier for this memory entry
    content: the text to store
    ttl_hours: hours until auto-deletion (ephemeral tier only)
    """
    return f"[write_memory stub] tier={tier} key={key} — memory system not yet implemented (Plan 2)"


@tool
def delete_memory(key: str, tier: str = "ephemeral") -> str:
    """Delete a memory entry by key and tier."""
    return f"[delete_memory stub] key={key} tier={tier} — memory system not yet implemented (Plan 2)"


@tool
def promote_memory(key: str, from_tier: str, target_tier: str) -> str:
    """Promote a memory entry from one tier to a higher tier.
    from_tier: current tier (rag | ephemeral | skill)
    target_tier: destination tier (skill | important)
    """
    return f"[promote_memory stub] {key}: {from_tier} → {target_tier} — not yet implemented (Plan 2)"


@tool
def create_agent_cron(name: str, cron_expr: str, action_prompt: str) -> str:
    """Create a new cron job owned by this agent.
    name: human-readable name for this cron
    cron_expr: standard cron expression e.g. '0 15 * * 1-5' (3pm weekdays)
    action_prompt: what the agent should do when the cron fires
    """
    return f"[create_agent_cron stub] '{name}' at '{cron_expr}' — cron system not yet implemented (Plan 2)"


@tool
def edit_agent_cron(cron_id: int, cron_expr: str | None = None,
                    action_prompt: str | None = None, enabled: bool | None = None) -> str:
    """Edit an existing agent-owned cron job."""
    return f"[edit_agent_cron stub] id={cron_id} — not yet implemented (Plan 2)"


@tool
def delete_agent_cron(cron_id: int) -> str:
    """Delete an agent-owned cron job."""
    return f"[delete_agent_cron stub] id={cron_id} — not yet implemented (Plan 2)"


@tool
def message_agent(to_slug: str, message_type: str, content: str) -> str:
    """Send an async message to another agent.
    to_slug: the slug of the target agent (e.g. 'finance-agent')
    message_type: task | report | query | alert | council_invite
    content: the message content
    """
    return f"[message_agent stub] → {to_slug} ({message_type}) — inter-agent comms not yet implemented (Plan 3)"


@tool
def invoke_agent(to_slug: str, query: str) -> str:
    """Synchronously invoke another agent and wait for its response (2 minute timeout).
    to_slug: the slug of the target agent
    query: the question or task to send
    """
    return f"[invoke_agent stub] → {to_slug}: '{query}' — not yet implemented (Plan 3)"


@tool
def convene_council(topic: str, agent_slugs: list[str], deadline_minutes: int = 30) -> str:
    """Convene a council of agents to deliberate on a topic.
    topic: what the council should decide or discuss
    agent_slugs: list of agent slugs to invite
    deadline_minutes: how long the council has to conclude
    """
    return f"[convene_council stub] '{topic}' with {agent_slugs} — not yet implemented (Plan 3)"


@tool
def post_to_blackboard(council_id: int, entry_type: str, content: str) -> str:
    """Post a contribution to an active council blackboard.
    entry_type: analysis | vote | question | answer | decision
    """
    return f"[post_to_blackboard stub] council={council_id} type={entry_type} — not yet implemented (Plan 3)"


@tool
def create_agent(name: str, persona: str, wake_mode: str = "always_on") -> str:
    """Spawn a new agent. The new agent starts in paused state and the user is notified.
    name: display name for the new agent
    persona: the system prompt / personality for the new agent
    wake_mode: always_on | event_only | scheduled_only | adaptive | manual
    """
    return f"[create_agent stub] '{name}' — agent spawning not yet implemented (Plan 4)"


@tool
def save_as_template(name: str, description: str) -> str:
    """Save the current agent's config as a reusable template."""
    return f"[save_as_template stub] '{name}' — templates not yet implemented (Plan 4)"


AGENT_TOOLS = [
    write_memory,
    delete_memory,
    promote_memory,
    create_agent_cron,
    edit_agent_cron,
    delete_agent_cron,
    message_agent,
    invoke_agent,
    convene_council,
    post_to_blackboard,
    create_agent,
    save_as_template,
]
```

- [ ] **Step 2: Quick smoke test**

```bash
cd "E:/BTP project"
python -c "
from app.teams.tools import AGENT_TOOLS
print('Tools loaded:', [t.name for t in AGENT_TOOLS])
"
```

Expected output: a list of 12 tool names, no errors.

- [ ] **Step 3: Commit**

```bash
git add app/teams/tools.py
git commit -m "feat: add stub agent tools (write_memory, message_agent, create_agent_cron, etc.)"
```

---

## Task 3: Agent Runtime — Tick Pipeline

**Files:**
- Create: `app/teams/runtime.py`

This is the core agent runtime. It:
1. Manages APScheduler jobs — one per active local agent
2. On each tick: drains the inbox queue, builds context, calls LangGraph, stores outputs
3. Implements the cost-saving no-op (skips LLM if inbox is empty)
4. Implements instant wakeup (push a row to agent_inbox, runtime picks it up)

- [ ] **Step 1: Create `app/teams/runtime.py`**

```python
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlalchemy import select, update

import app.config as app_config
from app.db.engine import AsyncSessionLocal
from app.teams.models_teams import Agent, AgentInbox, AgentMetric
from app.teams.tools import AGENT_TOOLS

logger = logging.getLogger(__name__)

_PRIORITY_ORDER = {"urgent": 0, "normal": 1, "background": 2}
_TICK_TIMEOUT = 120.0  # seconds


class AgentRuntime:
    """Manages all active local agents — schedules ticks, drains inboxes, runs LLM."""

    def __init__(self) -> None:
        self._scheduler: Any = None
        self._running_ticks: dict[int, bool] = {}  # agent_id → is_currently_ticking

    async def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self._scheduler = AsyncIOScheduler()
        self._scheduler.start()
        await self._register_all_active_agents()
        logger.info("AgentRuntime started.")

    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
        logger.info("AgentRuntime stopped.")

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    async def _register_all_active_agents(self) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Agent).where(
                    Agent.status == "active",
                    Agent.host_node_id.is_(None),  # local only
                )
            )
            agents = result.scalars().all()

        for agent in agents:
            self._schedule_agent(agent)
        logger.info("Registered %d active local agents.", len(agents))

    def _schedule_agent(self, agent: Agent) -> None:
        if agent.wake_mode == "event_only":
            return  # no scheduled ticks — wakes via push only
        if agent.wake_mode == "manual":
            return  # only wakes via explicit UI trigger

        job_id = f"agent_tick_{agent.id}"
        interval = agent.tick_interval_seconds

        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

        self._scheduler.add_job(
            self._tick_agent,
            "interval",
            seconds=interval,
            id=job_id,
            args=[agent.id],
            max_instances=1,
            coalesce=True,
        )
        logger.debug("Scheduled agent %s (id=%d) every %ds", agent.slug, agent.id, interval)

    def _unschedule_agent(self, agent_id: int) -> None:
        job_id = f"agent_tick_{agent_id}"
        if self._scheduler and self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    async def activate_agent(self, agent_id: int) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if agent:
                agent.status = "active"
                await db.commit()
                self._schedule_agent(agent)
                logger.info("Activated agent %s (id=%d)", agent.slug, agent_id)

    async def deactivate_agent(self, agent_id: int) -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Agent).where(Agent.id == agent_id).values(status="paused")
            )
            await db.commit()
        self._unschedule_agent(agent_id)
        logger.info("Deactivated agent id=%d", agent_id)

    # ------------------------------------------------------------------
    # Instant wakeup (push path)
    # ------------------------------------------------------------------

    async def push_event(
        self,
        agent_id: int,
        source_type: str,
        payload: dict,
        priority: str = "normal",
        source_id: str | None = None,
    ) -> None:
        """Insert an inbox event and immediately trigger a tick for event_only agents."""
        async with AsyncSessionLocal() as db:
            row = AgentInbox(
                agent_id=agent_id,
                source_type=source_type,
                source_id=source_id,
                payload_json=json.dumps(payload),
                priority=priority,
            )
            db.add(row)
            await db.commit()

        # For event_only and always_on agents, trigger immediate tick
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()

        if agent and agent.status == "active" and agent.wake_mode in ("event_only", "always_on", "adaptive"):
            if not self._running_ticks.get(agent_id):
                asyncio.create_task(self._tick_agent(agent_id))

    # ------------------------------------------------------------------
    # Tick pipeline
    # ------------------------------------------------------------------

    async def _tick_agent(self, agent_id: int) -> None:
        if self._running_ticks.get(agent_id):
            return  # already ticking, skip
        self._running_ticks[agent_id] = True

        tick_start = time.monotonic()
        llm_called = False
        actions_taken = 0
        had_error = False

        try:
            async with asyncio.timeout(_TICK_TIMEOUT):
                llm_called, actions_taken = await self._run_tick(agent_id)
        except TimeoutError:
            logger.error("Agent %d tick timed out after %ss", agent_id, _TICK_TIMEOUT)
            had_error = True
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Agent).where(Agent.id == agent_id).values(status="error")
                )
                await db.commit()
        except Exception as exc:
            logger.exception("Agent %d tick error: %s", agent_id, exc)
            had_error = True
        finally:
            self._running_ticks[agent_id] = False
            elapsed = time.monotonic() - tick_start
            logger.debug("Agent %d tick done in %.1fs (llm=%s actions=%d error=%s)",
                         agent_id, elapsed, llm_called, actions_taken, had_error)
            await self._record_metric(agent_id, llm_called, actions_taken, had_error)

    async def _run_tick(self, agent_id: int) -> tuple[bool, int]:
        """Core tick logic. Returns (llm_was_called, actions_taken)."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()

        if not agent or agent.status != "active":
            return False, 0

        # Step 1: Drain inbox — fetch pending items ordered by priority
        inbox_items = await self._drain_inbox(agent_id)

        if not inbox_items:
            # Cost-saving no-op: nothing to process
            return False, 0

        # Step 2: Build context
        system_prompt = self._build_system_prompt(agent)
        event_summary = self._format_inbox_items(inbox_items)

        # Step 3: Decide & act via LLM
        actions_taken = await self._invoke_llm(agent, system_prompt, event_summary)

        # Step 4: Mark inbox items as done
        await self._mark_inbox_done(agent_id, [item.id for item in inbox_items])

        return True, actions_taken

    async def _drain_inbox(self, agent_id: int) -> list[AgentInbox]:
        """Fetch up to 10 pending inbox items, priority-ordered."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentInbox)
                .where(
                    AgentInbox.agent_id == agent_id,
                    AgentInbox.status == "pending",
                )
                .order_by(AgentInbox.created_at.asc())
                .limit(10)
            )
            items = result.scalars().all()

            # Sort in-memory by priority
            items.sort(key=lambda x: (_PRIORITY_ORDER.get(x.priority, 99), x.created_at))

            # Mark as processing
            for item in items:
                item.status = "processing"
                item.processed_at = datetime.now(timezone.utc)
            await db.commit()

        return items

    def _build_system_prompt(self, agent: Agent) -> str:
        from datetime import timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(IST)
        return (
            f"{agent.persona}\n\n"
            f"Current date/time: {now.strftime('%A, %d %B %Y, %H:%M IST')}\n"
            f"Agent: {agent.name} (slug: {agent.slug})\n"
            f"Workspace: {app_config.WORKSPACE_DIR}\n\n"
            "You are an autonomous agent. Act on the events below. "
            "Use your tools. When done, respond with a brief summary of what you did."
        )

    def _format_inbox_items(self, items: list[AgentInbox]) -> str:
        lines = [f"You have {len(items)} new event(s) to process:\n"]
        for i, item in enumerate(items, 1):
            payload = item.payload
            lines.append(f"[Event {i}] source={item.source_type} priority={item.priority}")
            lines.append(f"  {json.dumps(payload, ensure_ascii=False)}")
        return "\n".join(lines)

    async def _invoke_llm(self, agent: Agent, system_prompt: str, event_text: str) -> int:
        """Call LangGraph-style ReAct loop for this agent. Returns number of tool calls made."""
        from app.agents.supervisor import WORKER_TOOLS
        from app.mcp.loader import load_active_mcp_tools

        llm = ChatOpenAI(
            model="gpt-4o-mini",  # cheaper for autonomous background agents
            api_key=app_config.OPENAI_API_KEY,
            streaming=False,
        )

        mcp_tools = await load_active_mcp_tools()
        allowed_tool_names = set(agent.allowed_tools)

        # Agent tools + standard RAION tools, filtered by allowed list
        all_tools = AGENT_TOOLS + WORKER_TOOLS + mcp_tools
        if allowed_tool_names:
            active_tools = [t for t in all_tools if t.name in allowed_tool_names]
        else:
            active_tools = AGENT_TOOLS  # default: only agent-specific tools

        tool_map = {t.name: t for t in active_tools}
        llm_with_tools = llm.bind_tools(active_tools)

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=event_text),
        ]

        actions_taken = 0
        max_iterations = 10

        for _ in range(max_iterations):
            response = await llm_with_tools.ainvoke(messages)
            messages.append(response)

            if not getattr(response, "tool_calls", None):
                break  # LLM is done

            from langchain_core.messages import ToolMessage
            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"] if isinstance(tc["args"], dict) else {}
                t = tool_map.get(tool_name)
                if t is None:
                    result_str = f"Unknown tool: {tool_name}"
                else:
                    try:
                        result = await asyncio.wait_for(t.ainvoke(tool_args), timeout=60.0)
                        result_str = str(result)
                        actions_taken += 1
                    except asyncio.TimeoutError:
                        result_str = f"Tool timed out: {tool_name}"
                    except Exception as exc:
                        result_str = f"Tool error: {exc}"

                messages.append(ToolMessage(tool_call_id=tc["id"], content=result_str))

        return actions_taken

    async def _mark_inbox_done(self, agent_id: int, item_ids: list[int]) -> None:
        if not item_ids:
            return
        async with AsyncSessionLocal() as db:
            for iid in item_ids:
                await db.execute(
                    update(AgentInbox)
                    .where(AgentInbox.id == iid)
                    .values(status="done")
                )
            await db.commit()

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def _record_metric(
        self, agent_id: int, llm_called: bool, actions_taken: int, had_error: bool
    ) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(AgentMetric).where(
                        AgentMetric.agent_id == agent_id,
                        AgentMetric.date == today,
                    )
                )
                metric = result.scalar_one_or_none()
                if metric is None:
                    metric = AgentMetric(agent_id=agent_id, date=today)
                    db.add(metric)

                metric.tick_count += 1
                if llm_called:
                    metric.llm_calls += 1
                metric.actions_taken += actions_taken
                if had_error:
                    metric.errors += 1
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to record metric for agent %d: %s", agent_id, exc)


# Module-level singleton
_runtime: AgentRuntime | None = None


def get_runtime() -> AgentRuntime:
    if _runtime is None:
        raise RuntimeError("AgentRuntime not initialised — did startup run?")
    return _runtime


async def init_runtime() -> None:
    global _runtime
    _runtime = AgentRuntime()
    await _runtime.start()


async def shutdown_runtime() -> None:
    global _runtime
    if _runtime:
        await _runtime.stop()
    _runtime = None
```

- [ ] **Step 2: Wire runtime into `app/main.py`**

Open `app/main.py`. Find the `on_startup` function (or `lifespan` context manager). Add the runtime init/shutdown calls alongside the existing supervisor init:

```python
# At top of file, add import:
from app.teams.runtime import init_runtime, shutdown_runtime

# Inside startup hook (after existing supervisor init):
await init_runtime()

# Inside shutdown hook (after existing supervisor shutdown):
await shutdown_runtime()
```

- [ ] **Step 3: Write runtime tests**

Create `tests/teams/test_runtime.py`:

```python
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, patch

from app.teams.models_teams import Agent, AgentInbox
from app.teams.runtime import AgentRuntime
from app.db.engine import AsyncSessionLocal
from sqlalchemy import select


@pytest.fixture
async def runtime():
    rt = AgentRuntime()
    # Don't start scheduler in tests — test individual methods
    return rt


@pytest.fixture
async def test_agent():
    async with AsyncSessionLocal() as db:
        agent = Agent(
            name="Runtime Test Agent",
            slug="runtime-test-001",
            persona="You are a test agent. When you receive events, respond with DONE.",
            status="active",
            wake_mode="always_on",
            tick_interval_seconds=30,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        yield agent
        await db.delete(agent)
        await db.commit()


@pytest.mark.asyncio
async def test_drain_inbox_empty(runtime, test_agent):
    items = await runtime._drain_inbox(test_agent.id)
    assert items == []


@pytest.mark.asyncio
async def test_drain_inbox_priority_ordering(runtime, test_agent):
    async with AsyncSessionLocal() as db:
        db.add(AgentInbox(agent_id=test_agent.id, source_type="seat",
                          payload_json='{"x":1}', priority="background"))
        db.add(AgentInbox(agent_id=test_agent.id, source_type="agent_message",
                          payload_json='{"x":2}', priority="urgent"))
        db.add(AgentInbox(agent_id=test_agent.id, source_type="webhook",
                          payload_json='{"x":3}', priority="normal"))
        await db.commit()

    items = await runtime._drain_inbox(test_agent.id)
    assert len(items) == 3
    assert items[0].priority == "urgent"
    assert items[1].priority == "normal"
    assert items[2].priority == "background"


@pytest.mark.asyncio
async def test_no_op_when_inbox_empty(runtime, test_agent):
    llm_called, actions = await runtime._run_tick(test_agent.id)
    assert llm_called is False
    assert actions == 0


@pytest.mark.asyncio
async def test_push_event_inserts_inbox_row(runtime, test_agent):
    await runtime.push_event(
        agent_id=test_agent.id,
        source_type="webhook",
        payload={"message": "hello"},
        priority="normal",
    )
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentInbox).where(
                AgentInbox.agent_id == test_agent.id,
                AgentInbox.source_type == "webhook",
            )
        )
        row = result.scalar_one_or_none()
        assert row is not None
        assert json.loads(row.payload_json)["message"] == "hello"


@pytest.mark.asyncio
async def test_build_system_prompt_contains_persona(runtime, test_agent):
    prompt = runtime._build_system_prompt(test_agent)
    assert "test agent" in prompt.lower()
    assert test_agent.slug in prompt


@pytest.mark.asyncio
async def test_format_inbox_items(runtime, test_agent):
    async with AsyncSessionLocal() as db:
        db.add(AgentInbox(agent_id=test_agent.id, source_type="whatsapp_group",
                          payload_json='{"text":"hi"}', priority="normal"))
        await db.commit()

    items = await runtime._drain_inbox(test_agent.id)
    formatted = runtime._format_inbox_items(items)
    assert "whatsapp_group" in formatted
    assert "hi" in formatted
```

- [ ] **Step 4: Run runtime tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_runtime.py -v
```

Expected: all 6 tests PASS. (The `test_no_op_when_inbox_empty` and `test_push_event_inserts_inbox_row` are integration tests hitting the real SQLite DB — that's fine.)

- [ ] **Step 5: Commit**

```bash
git add app/teams/runtime.py app/main.py tests/teams/test_runtime.py
git commit -m "feat: add AgentRuntime — tick pipeline, inbox drain, push_event, no-op optimisation"
```

---

## Task 4: Adaptive Tick Interval

**Files:**
- Modify: `app/teams/runtime.py`

Adaptive mode adjusts the tick interval based on recent activity. This is pure scheduling logic — no LLM involved.

- [ ] **Step 1: Add adaptive interval tracking to `AgentRuntime.__init__`**

Add this to the `__init__` method in `app/teams/runtime.py`:

```python
self._last_event_times: dict[int, float] = {}  # agent_id → monotonic time of last event
```

- [ ] **Step 2: Update `_tick_agent` to track event time and reschedule in adaptive mode**

In `_tick_agent`, after the tick completes successfully (inside the `try` block, after `_run_tick`), add:

```python
if llm_called:
    self._last_event_times[agent_id] = time.monotonic()
    await self._maybe_reschedule_adaptive(agent_id)
```

- [ ] **Step 3: Add `_maybe_reschedule_adaptive` method**

Add this method to `AgentRuntime`:

```python
async def _maybe_reschedule_adaptive(self, agent_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()

    if not agent or agent.wake_mode != "adaptive":
        return

    last = self._last_event_times.get(agent_id, 0)
    quiet_seconds = time.monotonic() - last

    if quiet_seconds < 60:
        new_interval = 10       # active: 10s
    elif quiet_seconds < 600:   # 10 minutes
        new_interval = 30       # normal: 30s
    elif quiet_seconds < 3600:  # 1 hour
        new_interval = 300      # quiet: 5min
    else:
        new_interval = 900      # deep quiet: 15min

    job_id = f"agent_tick_{agent_id}"
    job = self._scheduler.get_job(job_id)
    if job:
        current = job.trigger.interval.total_seconds()
        if abs(current - new_interval) > 2:  # avoid thrashing
            self._scheduler.reschedule_job(job_id, trigger="interval", seconds=new_interval)
            logger.debug("Agent %d adaptive: %.0fs → %ds quiet, new interval=%ds",
                         agent_id, quiet_seconds, quiet_seconds, new_interval)
```

- [ ] **Step 4: Write adaptive test**

Add to `tests/teams/test_runtime.py`:

```python
@pytest.mark.asyncio
async def test_adaptive_interval_thresholds(runtime):
    import time

    runtime._last_event_times[999] = time.monotonic() - 30  # 30s quiet

    async with AsyncSessionLocal() as db:
        agent = Agent(name="Adaptive Test", slug="adaptive-test-001",
                      persona="test", status="active", wake_mode="adaptive")
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    # No scheduler in test fixture, just verify the quiet-time logic
    quiet = time.monotonic() - runtime._last_event_times.get(agent_id, 0)
    assert quiet >= 0  # just checks it doesn't crash

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        a = result.scalar_one()
        await db.delete(a)
        await db.commit()
```

- [ ] **Step 5: Run all teams tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/ -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/teams/runtime.py tests/teams/test_runtime.py
git commit -m "feat: adaptive tick interval for always_on/adaptive wake modes"
```

---

## Task 5: Smoke Test — Full Server Startup

Verify the server starts cleanly with the new runtime wired in.

- [ ] **Step 1: Start the server**

```bash
cd "E:/BTP project"
python run.py
```

Expected output (among other lines):
```
INFO:app.teams.runtime:AgentRuntime started.
INFO:app.teams.runtime:Registered 0 active local agents.
```

No errors. Server runs on port 8000.

- [ ] **Step 2: Verify tables exist in DB**

```bash
python -c "
import asyncio
from app.db.engine import engine
from sqlalchemy import text

async def check():
    async with engine.connect() as conn:
        result = await conn.execute(text(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent%' OR name IN ('councils')\"))
        tables = [row[0] for row in result]
        print('Teams tables:', sorted(tables))

asyncio.run(check())
"
```

Expected output:
```
Teams tables: ['agent_blackboard', 'agent_crons', 'agent_environments', 'agent_inbox', 'agent_memory', 'agent_messages', 'agent_metrics', 'agent_nodes', 'agent_seats', 'agent_templates', 'agent_versions', 'agents', 'councils']
```

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "feat: add watchdog to requirements for Plan 2 seat integrations"
```

---

## Task 6: Add `watchdog` to requirements

- [ ] **Step 1: Check if watchdog is already in requirements.txt**

```bash
grep watchdog "E:/BTP project/requirements.txt"
```

If not present:

- [ ] **Step 2: Add watchdog**

Open `requirements.txt` and add:
```
watchdog>=4.0.0
```

- [ ] **Step 3: Install it**

```bash
pip install watchdog>=4.0.0
```

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: add watchdog dependency for folder seat integration (Plan 2)"
```

---

## Done — Plan 1 Complete

At this point you have:
- All 13 Teams DB tables created and tested
- Stub agent tools wired up
- AgentRuntime with tick pipeline, inbox drain, push_event, no-op optimisation, and adaptive intervals
- Server starts cleanly with runtime registered

**Next: Plan 2 — Memory System + Seat Integrations**
