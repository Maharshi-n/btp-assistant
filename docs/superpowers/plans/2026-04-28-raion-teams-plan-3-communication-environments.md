# RAION Teams — Plan 3: Inter-Agent Communication + Environments + Council

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement all three inter-agent communication modes (async messages, direct invocation, council/blackboard), the environment isolation system with System Agent, and the agent cron registry (agents creating their own cron jobs).

**Architecture:** `CommunicationManager` handles message routing (checking environment isolation, triggering instant wakeups), direct invocation (mini-tick with 2-minute timeout), and council orchestration. Environment isolation is enforced at the message layer — a helper checks if sender and receiver share an environment (or one is the System Agent) before allowing any message. Agent-owned crons are stored in `agent_crons` and managed by APScheduler slots in AgentRuntime.

**Tech Stack:** Python 3.11+, SQLAlchemy 2 async, APScheduler, asyncio, LangGraph (reused from existing supervisor for mini-ticks)

**Prerequisite:** Plans 1 and 2 must be fully implemented. `AgentRuntime`, `MemoryManager`, `SeatManager` are all running. Stub tools for `message_agent`, `invoke_agent`, `convene_council`, `create_agent_cron` exist in `app/teams/tools.py`.

**Implement in order. Do not skip tasks. Each task ends with a commit.**

---

## Codebase Context

- `app/teams/runtime.py` — `AgentRuntime` with `push_event()` and `_invoke_llm()`. You will call `_invoke_llm` for direct invocation mini-ticks.
- `app/teams/tools.py` — stubs for `message_agent`, `invoke_agent`, `convene_council`, `post_to_blackboard`, `create_agent_cron`, `edit_agent_cron`, `delete_agent_cron`. Replace all of these.
- `app/teams/models_teams.py` — `AgentMessage`, `Council`, `AgentBlackboard`, `AgentCron`, `Agent`, `AgentEnvironment` — all already created.
- `app/db/engine.py` — `AsyncSessionLocal`
- `current_agent_id` context var in `app/teams/tools.py` — set before any LLM call, used by tools to know which agent is calling.

---

## File Map

**New files:**
- `app/teams/comms.py` — `CommunicationManager`: message routing, direct invocation, council management
- `app/teams/agent_crons.py` — `AgentCronManager`: register/unregister agent-owned cron jobs in APScheduler
- `tests/teams/test_comms.py` — communication tests
- `tests/teams/test_agent_crons.py` — cron management tests

**Modified files:**
- `app/teams/tools.py` — replace stubs with real implementations calling `CommunicationManager` and `AgentCronManager`
- `app/teams/runtime.py` — integrate `AgentCronManager` into startup
- `app/main.py` — init `CommunicationManager`

---

## Task 1: Environment Isolation Helper

**Files:**
- Create: `app/teams/comms.py` (partial — just the isolation check)

- [ ] **Step 1: Create `app/teams/comms.py` with isolation check**

```python
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update

from app.db.engine import AsyncSessionLocal
from app.teams.models_teams import Agent, AgentBlackboard, AgentMessage, Council

logger = logging.getLogger(__name__)


async def _can_communicate(from_agent_id: Optional[int], to_agent_id: int) -> tuple[bool, str]:
    """
    Check if communication is allowed between two agents.
    Rules:
    - System agent (is_system_agent=True) can talk to anyone.
    - null from_agent_id = message from user, always allowed.
    - Agents in the same environment can talk freely.
    - Agents in different environments cannot talk (unless one is system agent).
    Returns (allowed, reason).
    """
    if from_agent_id is None:
        return True, "user"

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent).where(Agent.id.in_([from_agent_id, to_agent_id]))
        )
        agents = {a.id: a for a in result.scalars().all()}

    from_agent = agents.get(from_agent_id)
    to_agent = agents.get(to_agent_id)

    if not from_agent or not to_agent:
        return False, "one or both agents not found"

    # System agent bypasses all isolation
    if from_agent.is_system_agent or to_agent.is_system_agent:
        return True, "system_agent"

    # Same environment (including both null = no environment)
    if from_agent.environment_id == to_agent.environment_id:
        return True, "same_environment"

    return False, f"environment isolation: {from_agent.environment_id} vs {to_agent.environment_id}"


async def _resolve_agent_by_slug(slug: str) -> Optional[Agent]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.slug == slug))
        return result.scalar_one_or_none()
```

- [ ] **Step 2: Write isolation tests**

Create `tests/teams/test_comms.py`:

```python
from __future__ import annotations

import pytest
from app.teams.comms import _can_communicate
from app.teams.models_teams import Agent, AgentEnvironment
from app.db.engine import AsyncSessionLocal


@pytest.fixture
async def two_envs():
    async with AsyncSessionLocal() as db:
        env_a = AgentEnvironment(name="Env A", slug="env-a-001", color="#ff0000")
        env_b = AgentEnvironment(name="Env B", slug="env-b-001", color="#0000ff")
        db.add_all([env_a, env_b])
        await db.flush()

        agent_a = Agent(name="Agent A", slug="agent-a-001", persona="a",
                        environment_id=env_a.id, status="active")
        agent_b = Agent(name="Agent B", slug="agent-b-001", persona="b",
                        environment_id=env_b.id, status="active")
        agent_c = Agent(name="Agent C", slug="agent-c-001", persona="c",
                        environment_id=env_a.id, status="active")
        system = Agent(name="System", slug="system-001", persona="sys",
                       is_system_agent=True, status="active")
        db.add_all([agent_a, agent_b, agent_c, system])
        await db.commit()
        yield {"env_a": env_a, "env_b": env_b,
               "agent_a": agent_a, "agent_b": agent_b,
               "agent_c": agent_c, "system": system}

        await db.delete(agent_a)
        await db.delete(agent_b)
        await db.delete(agent_c)
        await db.delete(system)
        await db.delete(env_a)
        await db.delete(env_b)
        await db.commit()


@pytest.mark.asyncio
async def test_same_environment_allowed(two_envs):
    a = two_envs["agent_a"]
    c = two_envs["agent_c"]
    allowed, reason = await _can_communicate(a.id, c.id)
    assert allowed is True


@pytest.mark.asyncio
async def test_different_environment_blocked(two_envs):
    a = two_envs["agent_a"]
    b = two_envs["agent_b"]
    allowed, reason = await _can_communicate(a.id, b.id)
    assert allowed is False
    assert "environment isolation" in reason


@pytest.mark.asyncio
async def test_system_agent_bypasses_isolation(two_envs):
    sys = two_envs["system"]
    b = two_envs["agent_b"]
    allowed, _ = await _can_communicate(sys.id, b.id)
    assert allowed is True


@pytest.mark.asyncio
async def test_user_message_always_allowed(two_envs):
    b = two_envs["agent_b"]
    allowed, reason = await _can_communicate(None, b.id)
    assert allowed is True
    assert reason == "user"
```

- [ ] **Step 3: Run isolation tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_comms.py::test_same_environment_allowed tests/teams/test_comms.py::test_different_environment_blocked tests/teams/test_comms.py::test_system_agent_bypasses_isolation tests/teams/test_comms.py::test_user_message_always_allowed -v
```

Expected: all 4 PASS.

- [ ] **Step 4: Commit**

```bash
git add app/teams/comms.py tests/teams/test_comms.py
git commit -m "feat: environment isolation check — blocks cross-env comms, system agent bypasses"
```

---

## Task 2: Async Message Passing

**Files:**
- Modify: `app/teams/comms.py`

- [ ] **Step 1: Add `send_message` to `CommunicationManager` class in `app/teams/comms.py`**

Add this class after the helper functions:

```python
class CommunicationManager:
    """Handles all inter-agent communication: async messages, direct invocation, councils."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def send_message(
        self,
        from_agent_id: Optional[int],
        to_slug: str,
        message_type: str,
        content: str,
        thread_id: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Send an async message from one agent to another.
        Returns (success, error_or_message_id).
        """
        to_agent = await _resolve_agent_by_slug(to_slug)
        if not to_agent:
            return False, f"Agent '{to_slug}' not found"
        if to_agent.status not in ("active", "paused"):
            return False, f"Agent '{to_slug}' is {to_agent.status}"

        allowed, reason = await _can_communicate(from_agent_id, to_agent.id)
        if not allowed:
            return False, f"Communication blocked: {reason}"

        priority = "urgent" if message_type == "alert" else "normal"

        async with AsyncSessionLocal() as db:
            msg = AgentMessage(
                from_agent_id=from_agent_id,
                to_agent_id=to_agent.id,
                message_type=message_type,
                content=content,
                thread_id=thread_id or str(uuid.uuid4()),
                status="pending",
            )
            db.add(msg)
            await db.commit()
            msg_id = msg.id

        # Trigger instant wakeup on recipient
        await self._runtime.push_event(
            to_agent.id,
            "agent_message",
            {"from_agent_id": from_agent_id, "message_id": msg_id,
             "message_type": message_type, "content": content},
            priority=priority,
            source_id=str(msg_id),
        )

        return True, str(msg_id)
```

- [ ] **Step 2: Add message tests**

Add to `tests/teams/test_comms.py`:

```python
from app.teams.comms import CommunicationManager
from unittest.mock import AsyncMock, MagicMock
from app.teams.models_teams import AgentMessage
from sqlalchemy import select


@pytest.mark.asyncio
async def test_send_message_success(two_envs):
    mock_runtime = MagicMock()
    mock_runtime.push_event = AsyncMock()
    cm = CommunicationManager(mock_runtime)

    a = two_envs["agent_a"]
    c = two_envs["agent_c"]  # same environment as a

    success, result = await cm.send_message(a.id, c.slug, "task", "Process this invoice")
    assert success is True
    mock_runtime.push_event.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_blocked_cross_env(two_envs):
    mock_runtime = MagicMock()
    mock_runtime.push_event = AsyncMock()
    cm = CommunicationManager(mock_runtime)

    a = two_envs["agent_a"]
    b = two_envs["agent_b"]  # different environment

    success, reason = await cm.send_message(a.id, b.slug, "task", "Try to cross env")
    assert success is False
    assert "blocked" in reason.lower()
    mock_runtime.push_event.assert_not_called()


@pytest.mark.asyncio
async def test_send_alert_uses_urgent_priority(two_envs):
    mock_runtime = MagicMock()
    mock_runtime.push_event = AsyncMock()
    cm = CommunicationManager(mock_runtime)

    a = two_envs["agent_a"]
    c = two_envs["agent_c"]

    await cm.send_message(a.id, c.slug, "alert", "URGENT: server down")
    call_kwargs = mock_runtime.push_event.call_args.kwargs
    priority = call_kwargs.get("priority") or mock_runtime.push_event.call_args.args[3]
    assert priority == "urgent"
```

- [ ] **Step 3: Run message tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_comms.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/teams/comms.py tests/teams/test_comms.py
git commit -m "feat: async message passing between agents with environment isolation and instant wakeup"
```

---

## Task 3: Direct Invocation (Synchronous)

**Files:**
- Modify: `app/teams/comms.py`

- [ ] **Step 1: Add `invoke_agent` to `CommunicationManager`**

Add this method to the `CommunicationManager` class:

```python
_MAX_INVOKE_CHAIN_DEPTH = 3
_INVOKE_TIMEOUT = 120.0  # 2 minutes

async def invoke_agent(
    self,
    from_agent_id: int,
    to_slug: str,
    query: str,
    chain_depth: int = 0,
) -> str:
    """
    Synchronously invoke another agent and wait for response.
    Runs a mini-tick on the target agent with just this query.
    Returns the agent's response text or an error string.
    """
    if chain_depth >= self._MAX_INVOKE_CHAIN_DEPTH:
        return f"Error: max invocation chain depth ({self._MAX_INVOKE_CHAIN_DEPTH}) reached"

    to_agent = await _resolve_agent_by_slug(to_slug)
    if not to_agent:
        return f"Error: agent '{to_slug}' not found"
    if to_agent.status != "active":
        return f"Error: agent '{to_slug}' is not active"
    if to_agent.host_node_id is not None:
        return f"Error: agent '{to_slug}' is on a remote node — direct invocation not supported"

    allowed, reason = await _can_communicate(from_agent_id, to_agent.id)
    if not allowed:
        return f"Error: communication blocked — {reason}"

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == to_agent.id))
        target = result.scalar_one_or_none()

    if not target:
        return "Error: agent not found"

    from app.teams.memory import MemoryManager
    mm = MemoryManager()

    important = await mm.load_important(target.id)
    skills = await mm.load_skills_index(target.id)
    ephemeral = await mm.load_ephemeral(target.id)
    rag = await mm.search_rag(target.id, query, top_k=3)

    memory_blocks = (
        mm.format_important_block(important)
        + mm.format_ephemeral_block(ephemeral)
        + mm.format_skills_block(skills)
        + mm.format_rag_block(rag)
    )

    from datetime import timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)

    system_prompt = (
        f"{target.persona}\n\n"
        f"Date/time: {now.strftime('%A, %d %B %Y, %H:%M IST')}\n"
        f"You are being directly queried by another agent. "
        f"Answer the query directly and concisely. One response only."
        + memory_blocks
    )

    try:
        async with asyncio.timeout(self._INVOKE_TIMEOUT):
            actions = await self._runtime._invoke_llm(target, system_prompt, query)
    except TimeoutError:
        return f"Error: agent '{to_slug}' timed out after {self._INVOKE_TIMEOUT}s"
    except Exception as exc:
        return f"Error invoking agent '{to_slug}': {exc}"

    # Extract last LLM response — _invoke_llm returns action count not the text
    # We need to capture the response. Re-invoke lightweight for the response text.
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI
    import app.config as app_config

    llm = ChatOpenAI(model="gpt-4o-mini", api_key=app_config.OPENAI_API_KEY, streaming=False)
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=query)]
    response = await llm.ainvoke(messages)
    return response.content if hasattr(response, "content") else str(response)
```

- [ ] **Step 2: Add invocation tests**

Add to `tests/teams/test_comms.py`:

```python
@pytest.mark.asyncio
async def test_invoke_chain_depth_limit(two_envs):
    mock_runtime = MagicMock()
    mock_runtime._invoke_llm = AsyncMock(return_value=0)
    cm = CommunicationManager(mock_runtime)

    c = two_envs["agent_c"]
    result = await cm.invoke_agent(
        from_agent_id=two_envs["agent_a"].id,
        to_slug=c.slug,
        query="test",
        chain_depth=3,  # at limit
    )
    assert "max invocation chain depth" in result.lower()


@pytest.mark.asyncio
async def test_invoke_cross_env_blocked(two_envs):
    mock_runtime = MagicMock()
    cm = CommunicationManager(mock_runtime)

    a = two_envs["agent_a"]
    b = two_envs["agent_b"]
    result = await cm.invoke_agent(a.id, b.slug, "query")
    assert "blocked" in result.lower()
```

- [ ] **Step 3: Run tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_comms.py -v
```

Expected: all 9 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/teams/comms.py tests/teams/test_comms.py
git commit -m "feat: direct agent invocation — synchronous mini-tick with 2min timeout and chain depth limit"
```

---

## Task 4: Council + Blackboard

**Files:**
- Modify: `app/teams/comms.py`

- [ ] **Step 1: Add council methods to `CommunicationManager`**

Add these methods to `CommunicationManager`:

```python
async def convene_council(
    self,
    chair_agent_id: int,
    topic: str,
    agent_slugs: list[str],
    deadline_minutes: int = 30,
) -> tuple[bool, str]:
    """
    Create a council session and invite all specified agents.
    Returns (success, council_id_or_error).
    """
    chair = None
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == chair_agent_id))
        chair = result.scalar_one_or_none()

    if not chair:
        return False, "Chair agent not found"

    # Resolve all invited agents and check isolation
    invited = []
    for slug in agent_slugs:
        agent = await _resolve_agent_by_slug(slug)
        if not agent:
            return False, f"Agent '{slug}' not found"
        allowed, reason = await _can_communicate(chair_agent_id, agent.id)
        if not allowed:
            return False, f"Cannot invite '{slug}': {reason}"
        invited.append(agent)

    deadline_at = datetime.now(timezone.utc) + timedelta(minutes=deadline_minutes)

    async with AsyncSessionLocal() as db:
        council = Council(
            topic=topic,
            chair_agent_id=chair_agent_id,
            environment_id=chair.environment_id,
            status="active",
            deadline_at=deadline_at,
        )
        db.add(council)
        await db.flush()
        council_id = council.id

        # Write initial blackboard entry with topic
        db.add(AgentBlackboard(
            council_id=council_id,
            agent_id=chair_agent_id,
            entry_type="topic",
            content=topic,
        ))
        await db.commit()

    # Send council_invite to all participants
    for agent in invited:
        await self.send_message(
            from_agent_id=chair_agent_id,
            to_slug=agent.slug,
            message_type="council_invite",
            content=f"You have been invited to council #{council_id}: {topic}",
            thread_id=f"council_{council_id}",
        )

    return True, str(council_id)

async def post_to_blackboard(
    self,
    agent_id: int,
    council_id: int,
    entry_type: str,
    content: str,
) -> tuple[bool, str]:
    """Post a contribution to an active council blackboard."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Council).where(Council.id == council_id, Council.status == "active")
        )
        council = result.scalar_one_or_none()

    if not council:
        return False, f"Council #{council_id} not found or not active"

    async with AsyncSessionLocal() as db:
        entry = AgentBlackboard(
            council_id=council_id,
            agent_id=agent_id,
            entry_type=entry_type,
            content=content,
        )
        db.add(entry)
        await db.commit()

    return True, "posted"

async def get_blackboard(self, council_id: int) -> list[AgentBlackboard]:
    """Read all blackboard entries for a council, ordered by time."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentBlackboard)
            .where(AgentBlackboard.council_id == council_id)
            .order_by(AgentBlackboard.created_at.asc())
        )
        return result.scalars().all()

async def conclude_council(self, council_id: int) -> bool:
    """Mark a council as concluded."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Council).where(Council.id == council_id)
        )
        council = result.scalar_one_or_none()
        if not council:
            return False
        council.status = "concluded"
        council.concluded_at = datetime.now(timezone.utc)
        await db.commit()
    return True
```

- [ ] **Step 2: Add council tests**

Add to `tests/teams/test_comms.py`:

```python
@pytest.mark.asyncio
async def test_convene_council_creates_blackboard(two_envs):
    mock_runtime = MagicMock()
    mock_runtime.push_event = AsyncMock()
    cm = CommunicationManager(mock_runtime)

    a = two_envs["agent_a"]
    c = two_envs["agent_c"]

    success, council_id = await cm.convene_council(
        chair_agent_id=a.id,
        topic="Should we approve the budget?",
        agent_slugs=[c.slug],
        deadline_minutes=30,
    )
    assert success is True
    assert council_id.isdigit()

    entries = await cm.get_blackboard(int(council_id))
    assert len(entries) >= 1
    assert entries[0].entry_type == "topic"


@pytest.mark.asyncio
async def test_post_to_blackboard(two_envs):
    mock_runtime = MagicMock()
    mock_runtime.push_event = AsyncMock()
    cm = CommunicationManager(mock_runtime)

    a = two_envs["agent_a"]
    success, cid = await cm.convene_council(a.id, "Test council", [], 5)
    assert success

    ok, _ = await cm.post_to_blackboard(a.id, int(cid), "analysis", "Budget looks fine to me.")
    assert ok is True

    entries = await cm.get_blackboard(int(cid))
    analysis = [e for e in entries if e.entry_type == "analysis"]
    assert len(analysis) == 1
    assert "Budget" in analysis[0].content


@pytest.mark.asyncio
async def test_cross_env_council_invite_blocked(two_envs):
    mock_runtime = MagicMock()
    mock_runtime.push_event = AsyncMock()
    cm = CommunicationManager(mock_runtime)

    a = two_envs["agent_a"]
    b = two_envs["agent_b"]  # different env

    success, reason = await cm.convene_council(a.id, "Cross env council", [b.slug], 5)
    assert success is False
    assert "Cannot invite" in reason
```

- [ ] **Step 3: Run all comms tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_comms.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/teams/comms.py tests/teams/test_comms.py
git commit -m "feat: council system — convene, blackboard post/read, conclude, cross-env invite blocking"
```

---

## Task 5: Wire Communication Tools

**Files:**
- Modify: `app/teams/tools.py`
- Modify: `app/main.py`

- [ ] **Step 1: Add CommunicationManager singleton to `app/teams/comms.py`**

Add at the bottom of `app/teams/comms.py`:

```python
_comms: CommunicationManager | None = None


def get_comms() -> CommunicationManager:
    if _comms is None:
        raise RuntimeError("CommunicationManager not initialised")
    return _comms


async def init_comms(runtime: Any) -> None:
    global _comms
    _comms = CommunicationManager(runtime)


async def shutdown_comms() -> None:
    global _comms
    _comms = None
```

- [ ] **Step 2: Wire into `app/main.py`**

```python
from app.teams.comms import init_comms, shutdown_comms
from app.teams.runtime import get_runtime

# In startup (after init_runtime):
await init_comms(get_runtime())

# In shutdown:
await shutdown_comms()
```

- [ ] **Step 3: Replace communication stubs in `app/teams/tools.py`**

Replace the stub implementations for `message_agent`, `invoke_agent`, `convene_council`, `post_to_blackboard`:

```python
@tool
def message_agent(to_slug: str, message_type: str, content: str) -> str:
    """Send an async message to another agent.
    to_slug: slug of the target agent
    message_type: task | report | query | alert | council_invite
    content: message content
    """
    import asyncio
    from app.teams.comms import get_comms
    agent_id = current_agent_id.get() or None
    success, result = asyncio.get_event_loop().run_until_complete(
        get_comms().send_message(agent_id, to_slug, message_type, content)
    )
    if success:
        return f"Message sent to {to_slug} (id={result})"
    return f"Failed to send message: {result}"


@tool
def invoke_agent(to_slug: str, query: str) -> str:
    """Synchronously invoke another agent and wait for response (2 minute timeout).
    to_slug: slug of the target agent
    query: question or task to send
    """
    import asyncio
    from app.teams.comms import get_comms
    agent_id = current_agent_id.get()
    if not agent_id:
        return "Error: no agent context"
    result = asyncio.get_event_loop().run_until_complete(
        get_comms().invoke_agent(agent_id, to_slug, query)
    )
    return result


@tool
def convene_council(topic: str, agent_slugs: list[str], deadline_minutes: int = 30) -> str:
    """Convene a council of agents to deliberate on a topic.
    topic: what the council should decide or discuss
    agent_slugs: list of agent slugs to invite
    deadline_minutes: how long the council has to conclude
    """
    import asyncio
    from app.teams.comms import get_comms
    agent_id = current_agent_id.get()
    if not agent_id:
        return "Error: no agent context"
    success, result = asyncio.get_event_loop().run_until_complete(
        get_comms().convene_council(agent_id, topic, agent_slugs, deadline_minutes)
    )
    if success:
        return f"Council convened (id={result}). Invitations sent to: {', '.join(agent_slugs)}"
    return f"Failed to convene council: {result}"


@tool
def post_to_blackboard(council_id: int, entry_type: str, content: str) -> str:
    """Post a contribution to an active council blackboard.
    entry_type: analysis | vote | question | answer | decision
    """
    import asyncio
    from app.teams.comms import get_comms
    agent_id = current_agent_id.get()
    if not agent_id:
        return "Error: no agent context"
    success, result = asyncio.get_event_loop().run_until_complete(
        get_comms().post_to_blackboard(agent_id, council_id, entry_type, content)
    )
    return f"Posted to blackboard" if success else f"Failed: {result}"
```

- [ ] **Step 4: Commit**

```bash
git add app/teams/tools.py app/teams/comms.py app/main.py
git commit -m "feat: wire real message_agent, invoke_agent, convene_council, post_to_blackboard tools"
```

---

## Task 6: Agent-Owned Cron Registry

**Files:**
- Create: `app/teams/agent_crons.py`

Agents can create, edit, and delete their own cron jobs. These are stored in `agent_crons` and managed by APScheduler.

- [ ] **Step 1: Create `app/teams/agent_crons.py`**

```python
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update

from app.db.engine import AsyncSessionLocal
from app.teams.models_teams import Agent, AgentCron, AgentInbox

logger = logging.getLogger(__name__)


class AgentCronManager:
    """Manages agent-owned cron jobs in APScheduler."""

    def __init__(self, scheduler: Any, runtime: Any) -> None:
        self._scheduler = scheduler
        self._runtime = runtime

    async def load_all(self) -> None:
        """Register all enabled agent crons at startup."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentCron).where(AgentCron.enabled == True)
            )
            crons = result.scalars().all()

        for cron in crons:
            await self._register(cron)
        logger.info("Loaded %d agent cron jobs", len(crons))

    async def create(
        self,
        agent_id: int,
        name: str,
        cron_expr: str,
        action_prompt: str,
    ) -> AgentCron:
        async with AsyncSessionLocal() as db:
            cron = AgentCron(
                agent_id=agent_id,
                name=name,
                cron_expr=cron_expr,
                action_prompt=action_prompt,
                enabled=True,
            )
            db.add(cron)
            await db.commit()
            await db.refresh(cron)

        await self._register(cron)
        return cron

    async def edit(
        self,
        cron_id: int,
        cron_expr: Optional[str] = None,
        action_prompt: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> bool:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AgentCron).where(AgentCron.id == cron_id))
            cron = result.scalar_one_or_none()
            if not cron:
                return False
            if cron_expr:
                cron.cron_expr = cron_expr
            if action_prompt:
                cron.action_prompt = action_prompt
            if enabled is not None:
                cron.enabled = enabled
            await db.commit()
            await db.refresh(cron)

        # Re-register in scheduler
        self._unregister(cron_id)
        if cron.enabled:
            await self._register(cron)
        return True

    async def delete(self, cron_id: int) -> bool:
        self._unregister(cron_id)
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AgentCron).where(AgentCron.id == cron_id))
            cron = result.scalar_one_or_none()
            if not cron:
                return False
            await db.delete(cron)
            await db.commit()
        return True

    async def _register(self, cron: AgentCron) -> None:
        parts = cron.cron_expr.strip().split()
        if len(parts) != 5:
            logger.warning("Invalid cron_expr for agent cron %d: %s", cron.id, cron.cron_expr)
            return

        minute, hour, day, month, day_of_week = parts
        agent_id = cron.agent_id
        action_prompt = cron.action_prompt
        cron_id = cron.id
        runtime = self._runtime

        async def _fire():
            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(AgentCron)
                    .where(AgentCron.id == cron_id)
                    .values(last_run_at=now)
                )
                await db.commit()

            await runtime.push_event(
                agent_id,
                "agent_cron",
                {"cron_id": cron_id, "action_prompt": action_prompt},
                priority="normal",
                source_id=str(cron_id),
            )

        job_id = f"agent_cron_{cron_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

        self._scheduler.add_job(
            _fire,
            "cron",
            minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
            id=job_id,
            max_instances=1,
            coalesce=True,
        )
        logger.debug("Registered agent cron %d: %s", cron_id, cron.cron_expr)

    def _unregister(self, cron_id: int) -> None:
        job_id = f"agent_cron_{cron_id}"
        if self._scheduler and self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)


_cron_manager: AgentCronManager | None = None


def get_cron_manager() -> AgentCronManager:
    if _cron_manager is None:
        raise RuntimeError("AgentCronManager not initialised")
    return _cron_manager


async def init_cron_manager(scheduler: Any, runtime: Any) -> None:
    global _cron_manager
    _cron_manager = AgentCronManager(scheduler, runtime)
    await _cron_manager.load_all()
```

- [ ] **Step 2: Wire `AgentCronManager` into `AgentRuntime`**

In `app/teams/runtime.py`, in the `start` method, after the scheduler starts:

```python
from app.teams.agent_crons import init_cron_manager
await init_cron_manager(self._scheduler, self)
```

- [ ] **Step 3: Replace cron tool stubs in `app/teams/tools.py`**

```python
@tool
def create_agent_cron(name: str, cron_expr: str, action_prompt: str) -> str:
    """Create a new cron job owned by this agent.
    name: human-readable name
    cron_expr: standard 5-part cron expression e.g. '0 15 * * 1-5'
    action_prompt: what the agent should do when this cron fires
    """
    import asyncio
    from app.teams.agent_crons import get_cron_manager
    agent_id = current_agent_id.get()
    if not agent_id:
        return "Error: no agent context"
    cron = asyncio.get_event_loop().run_until_complete(
        get_cron_manager().create(agent_id, name, cron_expr, action_prompt)
    )
    return f"Cron created (id={cron.id}): '{name}' at '{cron_expr}'"


@tool
def edit_agent_cron(cron_id: int, cron_expr: str | None = None,
                    action_prompt: str | None = None, enabled: bool | None = None) -> str:
    """Edit an existing agent-owned cron job."""
    import asyncio
    from app.teams.agent_crons import get_cron_manager
    success = asyncio.get_event_loop().run_until_complete(
        get_cron_manager().edit(cron_id, cron_expr, action_prompt, enabled)
    )
    return f"Cron {cron_id} updated" if success else f"Cron {cron_id} not found"


@tool
def delete_agent_cron(cron_id: int) -> str:
    """Delete an agent-owned cron job."""
    import asyncio
    from app.teams.agent_crons import get_cron_manager
    success = asyncio.get_event_loop().run_until_complete(
        get_cron_manager().delete(cron_id)
    )
    return f"Cron {cron_id} deleted" if success else f"Cron {cron_id} not found"
```

- [ ] **Step 4: Write cron tests**

Create `tests/teams/test_agent_crons.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from app.teams.agent_crons import AgentCronManager
from app.teams.models_teams import Agent, AgentCron
from app.db.engine import AsyncSessionLocal
from sqlalchemy import select


@pytest.fixture
async def test_agent():
    async with AsyncSessionLocal() as db:
        a = Agent(name="Cron Test Agent", slug="cron-test-001", persona="test", status="active")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        yield a
        await db.delete(a)
        await db.commit()


@pytest.fixture
def mock_scheduler():
    s = MagicMock()
    s.get_job.return_value = None
    return s


@pytest.fixture
def mock_runtime():
    r = MagicMock()
    r.push_event = AsyncMock()
    return r


@pytest.mark.asyncio
async def test_create_cron(test_agent, mock_scheduler, mock_runtime):
    mgr = AgentCronManager(mock_scheduler, mock_runtime)
    cron = await mgr.create(
        agent_id=test_agent.id,
        name="Daily report",
        cron_expr="0 15 * * 1-5",
        action_prompt="Generate and send the daily campaign summary",
    )
    assert cron.id is not None
    assert cron.name == "Daily report"
    mock_scheduler.add_job.assert_called_once()


@pytest.mark.asyncio
async def test_edit_cron(test_agent, mock_scheduler, mock_runtime):
    mgr = AgentCronManager(mock_scheduler, mock_runtime)
    cron = await mgr.create(test_agent.id, "Test", "*/15 * * * *", "check stuff")

    success = await mgr.edit(cron.id, cron_expr="0 9 * * *")
    assert success is True

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AgentCron).where(AgentCron.id == cron.id))
        updated = result.scalar_one()
        assert updated.cron_expr == "0 9 * * *"


@pytest.mark.asyncio
async def test_delete_cron(test_agent, mock_scheduler, mock_runtime):
    mgr = AgentCronManager(mock_scheduler, mock_runtime)
    cron = await mgr.create(test_agent.id, "Deletable", "0 12 * * *", "do something")

    success = await mgr.delete(cron.id)
    assert success is True

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AgentCron).where(AgentCron.id == cron.id))
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_invalid_cron_expr_not_registered(test_agent, mock_scheduler, mock_runtime):
    mgr = AgentCronManager(mock_scheduler, mock_runtime)
    cron = await mgr.create(test_agent.id, "Bad cron", "not-a-cron", "do stuff")
    # Should create the DB row but not call scheduler.add_job
    mock_scheduler.add_job.assert_not_called()
```

- [ ] **Step 5: Run all tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/ -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/teams/agent_crons.py app/teams/tools.py app/teams/runtime.py tests/teams/test_agent_crons.py
git commit -m "feat: agent-owned cron registry — create/edit/delete crons, APScheduler integration, real tool implementations"
```

---

## Task 7: Smoke Test — Full System Integration

- [ ] **Step 1: Start server and verify all managers init**

```bash
cd "E:/BTP project"
python run.py
```

Expected log lines:
```
INFO:app.teams.runtime:AgentRuntime started.
INFO:app.teams.agent_crons:Loaded 0 agent cron jobs.
INFO:app.teams.seats:SeatManager started.
```

No errors.

- [ ] **Step 2: Run full test suite**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/ -v --tb=short
```

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add .
git commit -m "chore: Plan 3 complete — inter-agent comms, councils, environment isolation, agent crons all working"
```

---

## Done — Plan 3 Complete

At this point you have:
- Async message passing with environment isolation and instant wakeup
- Direct agent invocation with 2-minute timeout and chain depth limit
- Council system with blackboard, invitations, and cross-env blocking
- System Agent bypassing all isolation
- Agent-owned cron jobs stored in DB and managed in APScheduler
- All communication and cron tools fully implemented (no more stubs)

**Next: Plan 4 — Versioning + Templates + Metrics + Full UI**
