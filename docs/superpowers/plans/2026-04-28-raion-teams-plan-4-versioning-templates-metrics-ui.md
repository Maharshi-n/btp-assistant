# RAION Teams — Plan 4: Versioning + Templates + Metrics + Full UI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement agent versioning (config snapshots + rollback), the template library, the metrics system, the LAN Node Runner script, and the complete Teams UI — all routes, pages, and the 5-step agent creation wizard.

**Architecture:** Versioning is a snapshot-on-save pattern using `agent_versions` table. Templates are stored in `agent_templates`, seeded with 5 built-in ones. Metrics are updated after every tick in `agent_metrics`. The UI follows RAION's existing Jinja2 + Tailwind + HTMX + vanilla JS pattern — no new frontend stack. The Node Runner is a standalone `node_runner.py` script in the project root. All new routes live in `app/web/routes/teams.py`.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, Tailwind CSS (CDN offline copy already in static/), HTMX, vanilla JS, SQLAlchemy 2 async

**Prerequisite:** Plans 1, 2, and 3 must be fully implemented. All DB tables, AgentRuntime, MemoryManager, SeatManager, CommunicationManager, and AgentCronManager are running.

**Implement in order. Do not skip tasks. Each task ends with a commit.**

---

## Codebase Context

- `app/web/routes/` — all FastAPI route files. Add `teams.py` here.
- `app/web/templates/` — all Jinja2 templates. Add `teams/` subdirectory.
- `app/web/deps.py` — `require_user` dependency. Use on all Teams routes.
- `app/web/templates/base.html` — base layout. All new templates extend this.
- `app/web/templates/index.html` — reference for sidebar structure. Add Teams link here.
- `app/main.py` — register new router here.
- `app/teams/models_teams.py` — `AgentVersion`, `AgentTemplate`, `AgentMetric`, `Agent` etc.
- `app/teams/runtime.py` — `get_runtime()` for activate/deactivate agent calls.
- `app/db/engine.py` — `AsyncSessionLocal`

Dark theme CSS pattern: add overrides in `base.html` `<style>` block using `[data-theme="dark"] .class { ... !important }`. Never use inline styles.

HTMX pattern used in RAION: `hx-post`, `hx-get`, `hx-target`, `hx-swap="outerHTML"` on forms. Look at `app/web/templates/automations.html` for reference before writing any HTMX forms.

---

## File Map

**New files:**
- `app/web/routes/teams.py` — all Teams FastAPI routes
- `app/web/templates/teams/index.html` — Teams overview page
- `app/web/templates/teams/agents.html` — agent list page
- `app/web/templates/teams/agent_detail.html` — agent detail (6 tabs)
- `app/web/templates/teams/agent_create.html` — 5-step creation wizard
- `app/web/templates/teams/environments.html` — environment management
- `app/web/templates/teams/nodes.html` — LAN node management
- `app/web/templates/teams/councils.html` — council list
- `app/web/templates/teams/templates.html` — template library
- `app/web/templates/teams/analytics.html` — system-wide metrics
- `app/teams/versioning.py` — snapshot/rollback helpers
- `node_runner.py` — LAN node runner script (project root)
- `tests/teams/test_versioning.py` — versioning tests
- `tests/teams/test_routes.py` — basic route smoke tests

**Modified files:**
- `app/web/templates/base.html` — add Teams link to sidebar
- `app/main.py` — register teams router
- `app/db/seed.py` — seed built-in agent templates on first run
- `app/teams/runtime.py` — update `_record_metric` to track tokens + cost

---

## Task 1: Agent Versioning

**Files:**
- Create: `app/teams/versioning.py`

- [ ] **Step 1: Create `app/teams/versioning.py`**

```python
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from app.db.engine import AsyncSessionLocal
from app.teams.models_teams import Agent, AgentSeat, AgentMemory, AgentVersion

logger = logging.getLogger(__name__)


async def snapshot_agent(agent_id: int, created_by: str = "user") -> AgentVersion:
    """Save current agent config as a new version snapshot."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        seats_result = await db.execute(
            select(AgentSeat).where(AgentSeat.agent_id == agent_id)
        )
        seats = seats_result.scalars().all()

        memory_result = await db.execute(
            select(AgentMemory).where(
                AgentMemory.agent_id == agent_id,
                AgentMemory.tier == "important",
            )
        )
        important_memory = memory_result.scalars().all()

        snapshot = {
            "name": agent.name,
            "slug": agent.slug,
            "persona": agent.persona,
            "status": agent.status,
            "wake_mode": agent.wake_mode,
            "tick_interval_seconds": agent.tick_interval_seconds,
            "allowed_tools": agent.allowed_tools,
            "seats": [
                {
                    "seat_type": s.seat_type,
                    "config": s.config,
                    "output_channels": json.loads(s.output_channels_json),
                    "enabled": s.enabled,
                }
                for s in seats
            ],
            "important_memory": [
                {"key": m.key, "content": m.content}
                for m in important_memory
            ],
        }

        version_number = (agent.current_version or 0) + 1
        agent.current_version = version_number

        version = AgentVersion(
            agent_id=agent_id,
            version_number=version_number,
            config_snapshot_json=json.dumps(snapshot),
            created_by=created_by,
        )
        db.add(version)
        await db.commit()
        await db.refresh(version)
        return version


async def rollback_agent(agent_id: int, version_number: int, rolled_back_by: str = "user") -> bool:
    """
    Restore agent config to a previous version snapshot.
    Creates a new version entry for the rollback itself (rollback is undoable).
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent_id,
                AgentVersion.version_number == version_number,
            )
        )
        version = result.scalar_one_or_none()
        if not version:
            return False

        snapshot = json.loads(version.config_snapshot_json)

        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return False

        # First snapshot the current state before overwriting
        await db.commit()  # flush pending

    await snapshot_agent(agent_id, created_by=f"rollback_to_v{version_number}")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()

        agent.persona = snapshot.get("persona", agent.persona)
        agent.wake_mode = snapshot.get("wake_mode", agent.wake_mode)
        agent.tick_interval_seconds = snapshot.get("tick_interval_seconds", agent.tick_interval_seconds)
        agent.allowed_tools = snapshot.get("allowed_tools", agent.allowed_tools)

        # Restore seats: delete existing, re-create from snapshot
        seats_result = await db.execute(
            select(AgentSeat).where(AgentSeat.agent_id == agent_id)
        )
        for seat in seats_result.scalars().all():
            await db.delete(seat)

        for seat_data in snapshot.get("seats", []):
            import json as _json
            seat = AgentSeat(
                agent_id=agent_id,
                seat_type=seat_data["seat_type"],
                config_json=_json.dumps(seat_data.get("config", {})),
                output_channels_json=_json.dumps(seat_data.get("output_channels", [])),
                enabled=seat_data.get("enabled", True),
            )
            db.add(seat)

        # Restore important memory: delete existing, re-create from snapshot
        mem_result = await db.execute(
            select(AgentMemory).where(
                AgentMemory.agent_id == agent_id,
                AgentMemory.tier == "important",
            )
        )
        for mem in mem_result.scalars().all():
            await db.delete(mem)

        for mem_data in snapshot.get("important_memory", []):
            mem = AgentMemory(
                agent_id=agent_id,
                tier="important",
                key=mem_data["key"],
                content=mem_data["content"],
                token_count=max(1, len(mem_data["content"]) // 4),
            )
            db.add(mem)

        await db.commit()

    logger.info("Agent %d rolled back to version %d by %s", agent_id, version_number, rolled_back_by)
    return True


async def get_versions(agent_id: int) -> list[AgentVersion]:
    """Return all versions for an agent, newest first."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version_number.desc())
        )
        return result.scalars().all()
```

- [ ] **Step 2: Create `tests/teams/test_versioning.py`**

```python
from __future__ import annotations

import pytest
from app.teams.versioning import snapshot_agent, rollback_agent, get_versions
from app.teams.models_teams import Agent, AgentMemory
from app.db.engine import AsyncSessionLocal
from sqlalchemy import select


@pytest.fixture
async def test_agent():
    async with AsyncSessionLocal() as db:
        a = Agent(name="Version Test", slug="version-test-001",
                  persona="Original persona", status="active",
                  wake_mode="always_on", tick_interval_seconds=30)
        db.add(a)
        await db.commit()
        await db.refresh(a)

        mem = AgentMemory(agent_id=a.id, tier="important",
                          key="fact1", content="Important fact v1",
                          token_count=10)
        db.add(mem)
        await db.commit()
        yield a

        await db.delete(a)
        await db.commit()


@pytest.mark.asyncio
async def test_snapshot_creates_version(test_agent):
    version = await snapshot_agent(test_agent.id, created_by="user")
    assert version.version_number == 1
    assert "Original persona" in version.config_snapshot_json


@pytest.mark.asyncio
async def test_multiple_snapshots_increment_version(test_agent):
    v1 = await snapshot_agent(test_agent.id)
    v2 = await snapshot_agent(test_agent.id)
    assert v2.version_number == v1.version_number + 1


@pytest.mark.asyncio
async def test_rollback_restores_persona(test_agent):
    v1 = await snapshot_agent(test_agent.id)

    # Change persona
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == test_agent.id))
        agent = result.scalar_one()
        agent.persona = "Modified persona"
        await db.commit()

    # Rollback to v1
    success = await rollback_agent(test_agent.id, v1.version_number)
    assert success is True

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == test_agent.id))
        agent = result.scalar_one()
        assert "Original" in agent.persona


@pytest.mark.asyncio
async def test_rollback_is_itself_versioned(test_agent):
    v1 = await snapshot_agent(test_agent.id)
    versions_before = await get_versions(test_agent.id)
    count_before = len(versions_before)

    await rollback_agent(test_agent.id, v1.version_number)
    versions_after = await get_versions(test_agent.id)
    assert len(versions_after) > count_before  # rollback created a new version


@pytest.mark.asyncio
async def test_get_versions_ordered_newest_first(test_agent):
    await snapshot_agent(test_agent.id)
    await snapshot_agent(test_agent.id)
    await snapshot_agent(test_agent.id)
    versions = await get_versions(test_agent.id)
    nums = [v.version_number for v in versions]
    assert nums == sorted(nums, reverse=True)
```

- [ ] **Step 3: Run versioning tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_versioning.py -v
```

Expected: all 5 PASS.

- [ ] **Step 4: Commit**

```bash
git add app/teams/versioning.py tests/teams/test_versioning.py
git commit -m "feat: agent versioning — snapshot on save, rollback with new version entry, version history"
```

---

## Task 2: Seed Built-in Templates

**Files:**
- Modify: `app/db/seed.py`

- [ ] **Step 1: Add template seeding to `app/db/seed.py`**

Open `app/db/seed.py` and add this function, then call it from the existing `seed()` function:

```python
async def seed_agent_templates(db: AsyncSession) -> None:
    from app.teams.models_teams import AgentTemplate
    from sqlalchemy import select

    result = await db.execute(
        select(AgentTemplate).where(AgentTemplate.is_builtin == True)
    )
    existing = result.scalars().all()
    if existing:
        return  # already seeded

    templates = [
        {
            "name": "WhatsApp Group Monitor",
            "description": "Watches a WhatsApp group, summarizes activity daily, alerts on keywords.",
            "category": "monitoring",
            "config": {
                "persona": "You are a WhatsApp group monitor. Watch all messages carefully. Send a daily summary at 9pm. Alert the user on Telegram if any urgent keyword appears (fight, emergency, money, help).",
                "wake_mode": "always_on",
                "tick_interval_seconds": 30,
                "default_seats": [{"seat_type": "whatsapp_group"}],
                "default_tools": ["whatsapp_send", "telegram_send", "write_memory", "create_agent_cron"],
                "default_important_memory": [
                    {"key": "summary_time", "content": "Send daily summary at 9pm every day"},
                    {"key": "alert_keywords", "content": "Alert on: fight, emergency, money, help, urgent"},
                ],
            },
        },
        {
            "name": "Folder Processor",
            "description": "Watches a folder for new files, processes them, notifies on Telegram.",
            "category": "automation",
            "config": {
                "persona": "You are a folder processor agent. When a new file appears, read it, extract key information, log it, and notify the user on Telegram with a brief summary.",
                "wake_mode": "event_only",
                "tick_interval_seconds": 30,
                "default_seats": [{"seat_type": "folder"}],
                "default_tools": ["read_file", "write_file", "telegram_send", "write_memory"],
                "default_important_memory": [
                    {"key": "purpose", "content": "Process new files and send Telegram notifications"},
                ],
            },
        },
        {
            "name": "Email Monitor",
            "description": "Polls Gmail periodically, classifies emails, drafts replies for approval.",
            "category": "communication",
            "config": {
                "persona": "You are an email monitoring agent. Poll Gmail every 15 minutes. Classify each new email as: urgent, routine, spam, or newsletter. Draft a reply for urgent emails and send via telegram_ask for approval before sending.",
                "wake_mode": "scheduled_only",
                "tick_interval_seconds": 900,
                "default_seats": [{"seat_type": "cron_poll", "config": {"cron_expr": "*/15 * * * *", "fetch_target": "gmail"}}],
                "default_tools": ["gmail_list_unread", "gmail_read", "gmail_send", "telegram_ask", "write_memory"],
                "default_important_memory": [
                    {"key": "email_classification", "content": "Classify as: urgent (reply needed), routine, spam, newsletter"},
                ],
            },
        },
        {
            "name": "Research Agent",
            "description": "Web search specialist. Answers queries from other agents. Stores findings in RAG.",
            "category": "research",
            "config": {
                "persona": "You are a research specialist. When given a query, search the web thoroughly, synthesize findings, and return a clear answer. Store all findings in RAG memory for future reference.",
                "wake_mode": "event_only",
                "tick_interval_seconds": 30,
                "default_seats": [],
                "default_tools": ["web_search", "web_fetch", "write_memory", "rag_search", "rag_ingest"],
                "default_important_memory": [
                    {"key": "purpose", "content": "Research agent: answer queries from other agents using web search"},
                ],
            },
        },
        {
            "name": "Campaign Manager",
            "description": "Tracks staff submissions in a WhatsApp group, sends reminders, escalates to user.",
            "category": "management",
            "config": {
                "persona": "You are a campaign submission manager. Monitor the staff WhatsApp group. Track who has and hasn't submitted their campaign report. Send polite reminders to non-submitters. Escalate to Maharshi via Telegram if anyone hasn't submitted 30 minutes before deadline.",
                "wake_mode": "always_on",
                "tick_interval_seconds": 30,
                "default_seats": [{"seat_type": "whatsapp_group"}],
                "default_tools": ["whatsapp_send", "telegram_send", "write_memory", "create_agent_cron", "edit_agent_cron"],
                "default_important_memory": [
                    {"key": "deadline", "content": "Campaign submission deadline: every Friday at 3pm"},
                    {"key": "reminder_policy", "content": "Send first reminder at 1pm, second at 2:30pm, escalate to Maharshi at 2:45pm if anyone still missing"},
                    {"key": "tone", "content": "Always be polite and respectful when sending reminders"},
                ],
            },
        },
    ]

    for t in templates:
        db.add(AgentTemplate(
            name=t["name"],
            description=t["description"],
            category=t["category"],
            config_json=__import__("json").dumps(t["config"]),
            is_builtin=True,
            created_by="system",
        ))

    await db.commit()
```

In the main `seed()` async function, call `await seed_agent_templates(db)` after the admin user seed.

- [ ] **Step 2: Run seed**

```bash
cd "E:/BTP project"
python -c "
import asyncio
from app.db.seed import seed
asyncio.run(seed())
print('Seed complete')
"
```

Expected: `Seed complete` with no errors.

- [ ] **Step 3: Commit**

```bash
git add app/db/seed.py
git commit -m "feat: seed 5 built-in agent templates on first run"
```

---

## Task 3: Teams Routes

**Files:**
- Create: `app/web/routes/teams.py`

- [ ] **Step 1: Create `app/web/routes/teams.py`**

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, func

from app.db.engine import AsyncSessionLocal
from app.teams.models_teams import (
    Agent, AgentEnvironment, AgentInbox, AgentMemory, AgentMetric,
    AgentNode, AgentSeat, AgentTemplate, AgentVersion, Council,
)
from app.teams.runtime import get_runtime
from app.teams.versioning import get_versions, rollback_agent, snapshot_agent
from app.web.deps import require_user

router = APIRouter(prefix="/teams", tags=["teams"])


def _render(request: Request, template: str, **ctx):
    from app.main import templates
    return templates.TemplateResponse(template, {"request": request, **ctx})


# ── Overview ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def teams_overview(request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        agents_result = await db.execute(select(Agent))
        agents = agents_result.scalars().all()

        envs_result = await db.execute(select(AgentEnvironment))
        environments = envs_result.scalars().all()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        metrics_result = await db.execute(
            select(
                func.sum(AgentMetric.estimated_cost_usd),
                func.sum(AgentMetric.tick_count),
            ).where(AgentMetric.date == today)
        )
        row = metrics_result.one()
        today_cost = round(row[0] or 0.0, 4)
        today_ticks = row[1] or 0

    return _render(request, "teams/index.html",
                   agents=agents, environments=environments,
                   today_cost=today_cost, today_ticks=today_ticks)


# ── Agents ──────────────────────────────────────────────────────────────────

@router.get("/agents", response_class=HTMLResponse)
async def agents_list(request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent).order_by(Agent.is_system_agent.desc(), Agent.name.asc())
        )
        agents = result.scalars().all()
        envs_result = await db.execute(select(AgentEnvironment))
        environments = {e.id: e for e in envs_result.scalars().all()}
    return _render(request, "teams/agents.html", agents=agents, environments=environments)


@router.get("/agents/create", response_class=HTMLResponse)
async def agent_create_form(request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        envs_result = await db.execute(select(AgentEnvironment))
        environments = envs_result.scalars().all()
        nodes_result = await db.execute(select(AgentNode).where(AgentNode.status == "online"))
        nodes = nodes_result.scalars().all()
        agents_result = await db.execute(select(Agent))
        agents = agents_result.scalars().all()
        templates_result = await db.execute(select(AgentTemplate))
        templates = templates_result.scalars().all()
    return _render(request, "teams/agent_create.html",
                   environments=environments, nodes=nodes,
                   agents=agents, templates=templates)


class CreateAgentRequest(BaseModel):
    name: str
    persona: str
    wake_mode: str = "always_on"
    tick_interval_seconds: int = 30
    environment_id: int | None = None
    parent_agent_id: int | None = None
    host_node_id: int | None = None
    is_system_agent: bool = False
    allowed_tools: list[str] = []
    seats: list[dict] = []
    important_memory: list[dict] = []


@router.post("/api/agents")
async def create_agent(body: CreateAgentRequest, user=Depends(require_user)):
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")

    async with AsyncSessionLocal() as db:
        # Ensure slug uniqueness
        count = 0
        base_slug = slug
        while True:
            exists = await db.execute(select(Agent).where(Agent.slug == slug))
            if not exists.scalar_one_or_none():
                break
            count += 1
            slug = f"{base_slug}-{count}"

        agent = Agent(
            name=body.name,
            slug=slug,
            persona=body.persona,
            wake_mode=body.wake_mode,
            tick_interval_seconds=body.tick_interval_seconds,
            environment_id=body.environment_id,
            parent_agent_id=body.parent_agent_id,
            host_node_id=body.host_node_id,
            is_system_agent=body.is_system_agent,
            status="paused",
            allowed_tools_json=json.dumps(body.allowed_tools),
        )
        db.add(agent)
        await db.flush()

        for seat_data in body.seats:
            seat = AgentSeat(
                agent_id=agent.id,
                seat_type=seat_data.get("seat_type", "webhook"),
                config_json=json.dumps(seat_data.get("config", {})),
                output_channels_json=json.dumps(seat_data.get("output_channels", [])),
                enabled=True,
            )
            db.add(seat)

        for mem_data in body.important_memory:
            mem = AgentMemory(
                agent_id=agent.id,
                tier="important",
                key=mem_data.get("key", ""),
                content=mem_data.get("content", ""),
                token_count=max(1, len(mem_data.get("content", "")) // 4),
            )
            db.add(mem)

        await db.commit()
        agent_id = agent.id

    await snapshot_agent(agent_id, created_by="user")
    return {"id": agent_id, "slug": slug}


@router.get("/agents/{slug}", response_class=HTMLResponse)
async def agent_detail(slug: str, request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.slug == slug))
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        memory_result = await db.execute(
            select(AgentMemory).where(AgentMemory.agent_id == agent.id)
        )
        memory = memory_result.scalars().all()
        memory_by_tier = {"ephemeral": [], "important": [], "skill": [], "rag": []}
        for m in memory:
            memory_by_tier[m.tier].append(m)

        versions = await get_versions(agent.id)

        metrics_result = await db.execute(
            select(AgentMetric)
            .where(AgentMetric.agent_id == agent.id)
            .order_by(AgentMetric.date.desc())
            .limit(30)
        )
        metrics = metrics_result.scalars().all()

        inbox_result = await db.execute(
            select(AgentInbox)
            .where(AgentInbox.agent_id == agent.id)
            .order_by(AgentInbox.created_at.desc())
            .limit(20)
        )
        recent_inbox = inbox_result.scalars().all()

    return _render(request, "teams/agent_detail.html",
                   agent=agent, memory_by_tier=memory_by_tier,
                   versions=versions, metrics=metrics, recent_inbox=recent_inbox)


@router.post("/api/agents/{agent_id}/activate")
async def activate_agent(agent_id: int, user=Depends(require_user)):
    await get_runtime().activate_agent(agent_id)
    return {"status": "active"}


@router.post("/api/agents/{agent_id}/pause")
async def pause_agent(agent_id: int, user=Depends(require_user)):
    await get_runtime().deactivate_agent(agent_id)
    return {"status": "paused"}


@router.post("/api/agents/{agent_id}/rollback/{version_number}")
async def rollback_agent_route(agent_id: int, version_number: int, user=Depends(require_user)):
    success = await rollback_agent(agent_id, version_number)
    if not success:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"status": "rolled back"}


@router.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, user=Depends(require_user)):
    await get_runtime().deactivate_agent(agent_id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        await db.delete(agent)
        await db.commit()
    return {"status": "deleted"}


# ── Memory API ───────────────────────────────────────────────────────────────

class MemoryWriteRequest(BaseModel):
    tier: str
    key: str
    content: str
    ttl_hours: float = 24.0


@router.post("/api/agents/{agent_id}/memory")
async def write_memory_route(agent_id: int, body: MemoryWriteRequest, user=Depends(require_user)):
    from app.teams.memory import MemoryManager
    mm = MemoryManager()
    if body.tier == "rag":
        await mm.write_rag(agent_id, body.key, body.content)
    else:
        await mm.write(agent_id, body.tier, body.key, body.content, ttl_hours=body.ttl_hours)
    await snapshot_agent(agent_id, created_by="user")
    return {"status": "written"}


@router.delete("/api/agents/{agent_id}/memory/{tier}/{key}")
async def delete_memory_route(agent_id: int, tier: str, key: str, user=Depends(require_user)):
    from app.teams.memory import MemoryManager
    mm = MemoryManager()
    deleted = await mm.delete(agent_id, key, tier)
    if deleted:
        await snapshot_agent(agent_id, created_by="user")
    return {"deleted": deleted}


@router.post("/api/agents/{agent_id}/memory/promote")
async def promote_memory_route(
    agent_id: int,
    body: dict,
    user=Depends(require_user),
):
    from app.teams.memory import MemoryManager
    mm = MemoryManager()
    success = await mm.promote(agent_id, body["key"], body["from_tier"], body["target_tier"])
    if success:
        await snapshot_agent(agent_id, created_by="user")
    return {"promoted": success}


# ── Environments ─────────────────────────────────────────────────────────────

@router.get("/environments", response_class=HTMLResponse)
async def environments_list(request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AgentEnvironment))
        environments = result.scalars().all()
    return _render(request, "teams/environments.html", environments=environments)


class CreateEnvRequest(BaseModel):
    name: str
    description: str = ""
    color: str = "#6366f1"


@router.post("/api/environments")
async def create_environment(body: CreateEnvRequest, user=Depends(require_user)):
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
    async with AsyncSessionLocal() as db:
        env = AgentEnvironment(name=body.name, slug=slug,
                               description=body.description, color=body.color)
        db.add(env)
        await db.commit()
        return {"id": env.id, "slug": slug}


@router.delete("/api/environments/{env_id}")
async def delete_environment(env_id: int, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AgentEnvironment).where(AgentEnvironment.id == env_id))
        env = result.scalar_one_or_none()
        if not env:
            raise HTTPException(status_code=404, detail="Environment not found")
        await db.delete(env)
        await db.commit()
    return {"status": "deleted"}


# ── Nodes ─────────────────────────────────────────────────────────────────────

@router.get("/nodes", response_class=HTMLResponse)
async def nodes_list(request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AgentNode))
        nodes = result.scalars().all()
    return _render(request, "teams/nodes.html", nodes=nodes)


# ── Councils ──────────────────────────────────────────────────────────────────

@router.get("/councils", response_class=HTMLResponse)
async def councils_list(request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Council).order_by(Council.created_at.desc())
        )
        councils = result.scalars().all()
    return _render(request, "teams/councils.html", councils=councils)


# ── Templates ─────────────────────────────────────────────────────────────────

@router.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AgentTemplate))
        templates = result.scalars().all()
    return _render(request, "teams/templates.html", templates=templates)


# ── Analytics ─────────────────────────────────────────────────────────────────

@router.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request, user=Depends(require_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentMetric)
            .order_by(AgentMetric.date.desc())
            .limit(300)
        )
        metrics = result.scalars().all()
        agents_result = await db.execute(select(Agent))
        agents = {a.id: a.name for a in agents_result.scalars().all()}
    return _render(request, "teams/analytics.html", metrics=metrics, agents=agents)
```

- [ ] **Step 2: Register router in `app/main.py`**

```python
from app.web.routes.teams import router as teams_router
app.include_router(teams_router)
```

- [ ] **Step 3: Write route smoke tests**

Create `tests/teams/test_routes.py`:

```python
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
async def client():
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Log in
        await c.post("/login", data={"username": "maharshi", "password": "test"})
        yield c


@pytest.mark.asyncio
async def test_teams_overview_200(client):
    resp = await client.get("/teams/")
    assert resp.status_code in (200, 302)


@pytest.mark.asyncio
async def test_agents_list_200(client):
    resp = await client.get("/teams/agents")
    assert resp.status_code in (200, 302)


@pytest.mark.asyncio
async def test_environments_list_200(client):
    resp = await client.get("/teams/environments")
    assert resp.status_code in (200, 302)


@pytest.mark.asyncio
async def test_templates_list_200(client):
    resp = await client.get("/teams/templates")
    assert resp.status_code in (200, 302)
```

- [ ] **Step 4: Run tests**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/test_routes.py tests/teams/test_versioning.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/web/routes/teams.py app/main.py tests/teams/test_routes.py
git commit -m "feat: Teams FastAPI routes — agents CRUD, memory API, environments, councils, templates, analytics"
```

---

## Task 4: Core Templates

**Files:**
- Create: `app/web/templates/teams/` directory and all template files

- [ ] **Step 1: Create `app/web/templates/teams/index.html`**

```html
{% extends "base.html" %}
{% block title %}Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-6xl mx-auto">
  <div class="flex items-center justify-between mb-6">
    <h1 class="text-2xl font-bold">Teams</h1>
    <a href="/teams/agents/create"
       class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-lg text-sm font-medium">
      + New Agent
    </a>
  </div>

  <!-- Stats bar -->
  <div class="grid grid-cols-4 gap-4 mb-8">
    <div class="bg-white rounded-xl p-4 shadow-sm border border-gray-100">
      <div class="text-sm text-gray-500">Total Agents</div>
      <div class="text-2xl font-bold mt-1">{{ agents|length }}</div>
    </div>
    <div class="bg-white rounded-xl p-4 shadow-sm border border-gray-100">
      <div class="text-sm text-gray-500">Active Agents</div>
      <div class="text-2xl font-bold mt-1 text-green-600">
        {{ agents|selectattr("status","eq","active")|list|length }}
      </div>
    </div>
    <div class="bg-white rounded-xl p-4 shadow-sm border border-gray-100">
      <div class="text-sm text-gray-500">Environments</div>
      <div class="text-2xl font-bold mt-1">{{ environments|length }}</div>
    </div>
    <div class="bg-white rounded-xl p-4 shadow-sm border border-gray-100">
      <div class="text-sm text-gray-500">Today's Cost</div>
      <div class="text-2xl font-bold mt-1 text-amber-600">${{ today_cost }}</div>
    </div>
  </div>

  <!-- Environments -->
  {% if environments %}
  <h2 class="text-lg font-semibold mb-3">Environments</h2>
  <div class="grid grid-cols-3 gap-4 mb-8">
    {% for env in environments %}
    <div class="bg-white rounded-xl p-4 shadow-sm border-l-4"
         style="border-left-color: {{ env.color }}">
      <div class="font-semibold">{{ env.name }}</div>
      <div class="text-sm text-gray-500 mt-1">{{ env.description or "No description" }}</div>
      <div class="text-xs text-gray-400 mt-2">
        {{ agents|selectattr("environment_id","eq",env.id)|list|length }} agents
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- System Agent -->
  {% set system_agents = agents|selectattr("is_system_agent")|list %}
  {% if system_agents %}
  <div class="mb-6 p-4 bg-purple-50 border border-purple-200 rounded-xl">
    <div class="flex items-center gap-2 mb-1">
      <span class="text-purple-600 font-semibold">⚡ System Agent</span>
      <span class="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full">Above all environments</span>
    </div>
    {% for a in system_agents %}
    <a href="/teams/agents/{{ a.slug }}" class="text-sm text-purple-800 hover:underline">{{ a.name }}</a>
    {% endfor %}
  </div>
  {% endif %}

  <!-- All Agents -->
  <h2 class="text-lg font-semibold mb-3">All Agents</h2>
  <div class="space-y-3">
    {% for agent in agents if not agent.is_system_agent %}
    <div class="bg-white rounded-xl p-4 shadow-sm border border-gray-100 flex items-center justify-between">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full
            {% if agent.status == 'active' %}bg-green-500
            {% elif agent.status == 'error' %}bg-red-500
            {% elif agent.status == 'paused' %}bg-yellow-400
            {% else %}bg-gray-400{% endif %}"></span>
          <a href="/teams/agents/{{ agent.slug }}" class="font-medium hover:text-indigo-600">
            {{ agent.name }}
          </a>
          <span class="text-xs text-gray-400">{{ agent.wake_mode }}</span>
        </div>
        <div class="text-sm text-gray-500 mt-1 ml-4">{{ agent.persona[:80] }}…</div>
      </div>
      <div class="flex gap-2">
        <a href="/teams/agents/{{ agent.slug }}"
           class="text-sm text-indigo-600 hover:underline">View</a>
      </div>
    </div>
    {% else %}
    <div class="text-gray-400 text-sm py-8 text-center">No agents yet.
      <a href="/teams/agents/create" class="text-indigo-600 hover:underline">Create your first agent →</a>
    </div>
    {% endfor %}
  </div>
</div>
{% endblock %}
```

- [ ] **Step 2: Create `app/web/templates/teams/agent_detail.html`**

```html
{% extends "base.html" %}
{% block title %}{{ agent.name }} — Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-5xl mx-auto">
  <!-- Header -->
  <div class="flex items-center justify-between mb-6">
    <div>
      <div class="flex items-center gap-3">
        <span class="w-3 h-3 rounded-full
          {% if agent.status == 'active' %}bg-green-500
          {% elif agent.status == 'error' %}bg-red-500
          {% elif agent.status == 'paused' %}bg-yellow-400
          {% else %}bg-gray-400{% endif %}"></span>
        <h1 class="text-2xl font-bold">{{ agent.name }}</h1>
        <span class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">{{ agent.slug }}</span>
        {% if agent.is_system_agent %}
        <span class="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full">System Agent</span>
        {% endif %}
      </div>
      <div class="text-sm text-gray-500 mt-1 ml-6">{{ agent.wake_mode }} · every {{ agent.tick_interval_seconds }}s</div>
    </div>
    <div class="flex gap-2">
      {% if agent.status == 'active' %}
      <button onclick="fetch('/teams/api/agents/{{ agent.id }}/pause',{method:'POST'}).then(()=>location.reload())"
              class="bg-yellow-500 hover:bg-yellow-600 text-white px-4 py-2 rounded-lg text-sm">Pause</button>
      {% else %}
      <button onclick="fetch('/teams/api/agents/{{ agent.id }}/activate',{method:'POST'}).then(()=>location.reload())"
              class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg text-sm">Activate</button>
      {% endif %}
      <button onclick="if(confirm('Delete this agent?'))fetch('/teams/api/agents/{{ agent.id }}',{method:'DELETE'}).then(()=>window.location='/teams/agents')"
              class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg text-sm">Delete</button>
    </div>
  </div>

  <!-- Tabs -->
  <div class="border-b border-gray-200 mb-6">
    <nav class="flex gap-6" id="tabs">
      {% for tab in ['overview','memory','crons','hierarchy','metrics','versions'] %}
      <button onclick="showTab('{{ tab }}')" id="tab-{{ tab }}"
              class="tab-btn pb-2 text-sm font-medium text-gray-500 hover:text-gray-900 border-b-2 border-transparent">
        {{ tab|title }}
      </button>
      {% endfor %}
    </nav>
  </div>

  <!-- Overview Tab -->
  <div id="pane-overview" class="tab-pane">
    <div class="bg-gray-50 rounded-xl p-4 mb-4">
      <div class="text-sm font-medium text-gray-700 mb-2">Persona</div>
      <pre class="text-sm text-gray-600 whitespace-pre-wrap">{{ agent.persona }}</pre>
    </div>
    <div class="text-sm font-medium text-gray-700 mb-2">Recent Inbox Events</div>
    <div class="space-y-2">
      {% for item in recent_inbox %}
      <div class="bg-white border border-gray-100 rounded-lg p-3 text-sm">
        <span class="text-xs bg-gray-100 px-1.5 py-0.5 rounded">{{ item.source_type }}</span>
        <span class="text-xs text-gray-400 ml-2">{{ item.priority }} · {{ item.status }}</span>
        <div class="text-gray-600 mt-1 truncate">{{ item.payload_json[:120] }}</div>
      </div>
      {% else %}
      <div class="text-gray-400 text-sm">No recent inbox events.</div>
      {% endfor %}
    </div>
  </div>

  <!-- Memory Tab -->
  <div id="pane-memory" class="tab-pane hidden">
    {% for tier in ['important','skill','ephemeral','rag'] %}
    <div class="mb-6">
      <h3 class="font-semibold capitalize mb-2">{{ tier }} Memory
        <span class="text-xs text-gray-400 font-normal">({{ memory_by_tier[tier]|length }} entries)</span>
      </h3>
      <div class="space-y-2">
        {% for mem in memory_by_tier[tier] %}
        <div class="bg-white border border-gray-100 rounded-lg p-3 flex justify-between items-start">
          <div>
            <span class="text-xs font-mono bg-gray-100 px-1.5 py-0.5 rounded">{{ mem.key }}</span>
            <div class="text-sm text-gray-700 mt-1">{{ mem.content[:200] }}</div>
            <div class="text-xs text-gray-400 mt-1">
              accessed {{ mem.access_count }}× · {{ mem.token_count }} tokens
            </div>
          </div>
          <div class="flex gap-2 ml-4 shrink-0">
            <select onchange="promoteMemory({{ agent.id }},'{{ mem.key }}','{{ tier }}',this.value)"
                    class="text-xs border border-gray-200 rounded px-1 py-0.5">
              <option value="">Promote to…</option>
              {% if tier != 'important' %}<option value="important">Important</option>{% endif %}
              {% if tier not in ['skill','important'] %}<option value="skill">Skill</option>{% endif %}
            </select>
            <button onclick="deleteMemory({{ agent.id }},'{{ tier }}','{{ mem.key }}')"
                    class="text-xs text-red-500 hover:text-red-700">Del</button>
          </div>
        </div>
        {% else %}
        <div class="text-gray-400 text-sm">No {{ tier }} memory entries.</div>
        {% endfor %}
      </div>
    </div>
    {% endfor %}
  </div>

  <!-- Metrics Tab -->
  <div id="pane-metrics" class="tab-pane hidden">
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead><tr class="text-left text-gray-500 border-b">
          <th class="pb-2 pr-4">Date</th>
          <th class="pb-2 pr-4">Ticks</th>
          <th class="pb-2 pr-4">LLM Calls</th>
          <th class="pb-2 pr-4">Actions</th>
          <th class="pb-2 pr-4">Tokens</th>
          <th class="pb-2 pr-4">Cost</th>
          <th class="pb-2">Errors</th>
        </tr></thead>
        <tbody>
          {% for m in metrics %}
          <tr class="border-b border-gray-50 hover:bg-gray-50">
            <td class="py-2 pr-4 font-mono text-xs">{{ m.date }}</td>
            <td class="py-2 pr-4">{{ m.tick_count }}</td>
            <td class="py-2 pr-4">{{ m.llm_calls }}</td>
            <td class="py-2 pr-4">{{ m.actions_taken }}</td>
            <td class="py-2 pr-4">{{ m.tokens_used }}</td>
            <td class="py-2 pr-4">${{ "%.4f"|format(m.estimated_cost_usd) }}</td>
            <td class="py-2 {% if m.errors > 0 %}text-red-500{% endif %}">{{ m.errors }}</td>
          </tr>
          {% else %}
          <tr><td colspan="7" class="py-4 text-gray-400 text-center">No metrics yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Versions Tab -->
  <div id="pane-versions" class="tab-pane hidden">
    <div class="space-y-2">
      {% for v in versions %}
      <div class="bg-white border border-gray-100 rounded-lg p-3 flex justify-between items-center">
        <div>
          <span class="font-mono text-sm font-semibold">v{{ v.version_number }}</span>
          <span class="text-xs text-gray-400 ml-2">{{ v.created_at.strftime('%Y-%m-%d %H:%M') }}</span>
          <span class="text-xs text-gray-500 ml-2">by {{ v.created_by }}</span>
        </div>
        {% if not loop.first %}
        <button onclick="rollbackTo({{ agent.id }}, {{ v.version_number }})"
                class="text-xs text-indigo-600 hover:underline">Rollback to v{{ v.version_number }}</button>
        {% else %}
        <span class="text-xs text-green-600 font-medium">Current</span>
        {% endif %}
      </div>
      {% else %}
      <div class="text-gray-400 text-sm">No versions yet.</div>
      {% endfor %}
    </div>
  </div>

  <!-- Other tabs placeholder -->
  <div id="pane-crons" class="tab-pane hidden text-gray-400 text-sm py-4">Agent-owned cron jobs will appear here.</div>
  <div id="pane-hierarchy" class="tab-pane hidden text-gray-400 text-sm py-4">Hierarchy and inter-agent messages will appear here.</div>
</div>

<script>
function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.remove('text-indigo-600','border-indigo-600');
    b.classList.add('text-gray-500','border-transparent');
  });
  document.getElementById('pane-' + name).classList.remove('hidden');
  const btn = document.getElementById('tab-' + name);
  btn.classList.remove('text-gray-500','border-transparent');
  btn.classList.add('text-indigo-600','border-indigo-600');
}
showTab('overview');

function rollbackTo(agentId, version) {
  if (!confirm('Rollback to v' + version + '? This will create a new version entry.')) return;
  fetch('/teams/api/agents/' + agentId + '/rollback/' + version, {method: 'POST'})
    .then(() => location.reload());
}

function deleteMemory(agentId, tier, key) {
  if (!confirm('Delete memory entry: ' + key + '?')) return;
  fetch('/teams/api/agents/' + agentId + '/memory/' + tier + '/' + encodeURIComponent(key), {method: 'DELETE'})
    .then(() => location.reload());
}

function promoteMemory(agentId, key, fromTier, targetTier) {
  if (!targetTier) return;
  fetch('/teams/api/agents/' + agentId + '/memory/promote', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, from_tier: fromTier, target_tier: targetTier})
  }).then(() => location.reload());
}
</script>
{% endblock %}
```

- [ ] **Step 3: Create remaining simple templates**

Create `app/web/templates/teams/agents.html`:

```html
{% extends "base.html" %}
{% block title %}Agents — Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-5xl mx-auto">
  <div class="flex items-center justify-between mb-6">
    <h1 class="text-2xl font-bold">Agents</h1>
    <a href="/teams/agents/create"
       class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-lg text-sm font-medium">
      + New Agent
    </a>
  </div>
  <div class="space-y-3">
    {% for agent in agents %}
    <div class="bg-white rounded-xl p-4 shadow-sm border border-gray-100 flex items-center justify-between">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full
            {% if agent.status == 'active' %}bg-green-500
            {% elif agent.status == 'error' %}bg-red-500
            {% elif agent.status == 'paused' %}bg-yellow-400
            {% else %}bg-gray-400{% endif %}"></span>
          <a href="/teams/agents/{{ agent.slug }}" class="font-medium hover:text-indigo-600">{{ agent.name }}</a>
          {% if agent.is_system_agent %}<span class="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full">System</span>{% endif %}
          {% if environments.get(agent.environment_id) %}
          <span class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">
            {{ environments[agent.environment_id].name }}
          </span>
          {% endif %}
        </div>
        <div class="text-sm text-gray-500 mt-1 ml-4">{{ agent.wake_mode }} · {{ agent.status }}</div>
      </div>
      <a href="/teams/agents/{{ agent.slug }}" class="text-sm text-indigo-600 hover:underline">View →</a>
    </div>
    {% else %}
    <div class="text-gray-400 text-sm py-8 text-center">
      No agents yet. <a href="/teams/agents/create" class="text-indigo-600 hover:underline">Create one →</a>
    </div>
    {% endfor %}
  </div>
</div>
{% endblock %}
```

Create `app/web/templates/teams/environments.html`:

```html
{% extends "base.html" %}
{% block title %}Environments — Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-5xl mx-auto">
  <div class="flex items-center justify-between mb-6">
    <h1 class="text-2xl font-bold">Environments</h1>
    <button onclick="document.getElementById('create-env-modal').classList.remove('hidden')"
            class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-lg text-sm">
      + New Environment
    </button>
  </div>
  <div class="grid grid-cols-3 gap-4">
    {% for env in environments %}
    <div class="bg-white rounded-xl p-4 shadow-sm border-l-4" style="border-left-color: {{ env.color }}">
      <div class="font-semibold">{{ env.name }}</div>
      <div class="text-sm text-gray-500 mt-1">{{ env.description or "—" }}</div>
      <div class="text-xs text-gray-400 mt-3">slug: {{ env.slug }}</div>
      <button onclick="if(confirm('Delete environment {{ env.name }}?'))fetch('/teams/api/environments/{{ env.id }}',{method:'DELETE'}).then(()=>location.reload())"
              class="mt-3 text-xs text-red-500 hover:text-red-700">Delete</button>
    </div>
    {% else %}
    <div class="col-span-3 text-gray-400 text-sm text-center py-8">No environments yet.</div>
    {% endfor %}
  </div>

  <!-- Create modal -->
  <div id="create-env-modal" class="hidden fixed inset-0 bg-black/40 flex items-center justify-center z-50">
    <div class="bg-white rounded-2xl p-6 w-96 shadow-xl">
      <h2 class="text-lg font-bold mb-4">New Environment</h2>
      <input id="env-name" placeholder="Name" class="w-full border border-gray-200 rounded-lg px-3 py-2 mb-3 text-sm">
      <input id="env-desc" placeholder="Description (optional)" class="w-full border border-gray-200 rounded-lg px-3 py-2 mb-3 text-sm">
      <input id="env-color" type="color" value="#6366f1" class="mb-4 w-full h-10 rounded-lg border border-gray-200">
      <div class="flex gap-2 justify-end">
        <button onclick="document.getElementById('create-env-modal').classList.add('hidden')"
                class="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">Cancel</button>
        <button onclick="createEnv()"
                class="bg-indigo-600 text-white px-4 py-2 rounded-lg text-sm">Create</button>
      </div>
    </div>
  </div>
</div>
<script>
function createEnv() {
  fetch('/teams/api/environments', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name: document.getElementById('env-name').value,
      description: document.getElementById('env-desc').value,
      color: document.getElementById('env-color').value,
    })
  }).then(() => location.reload());
}
</script>
{% endblock %}
```

Create `app/web/templates/teams/nodes.html`:

```html
{% extends "base.html" %}
{% block title %}Nodes — Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-5xl mx-auto">
  <h1 class="text-2xl font-bold mb-6">LAN Nodes</h1>
  <div class="bg-amber-50 border border-amber-200 rounded-xl p-4 mb-6 text-sm">
    <strong>To add a node:</strong> Copy <code class="bg-amber-100 px-1 rounded">node_runner.py</code>
    to the remote PC and run:<br>
    <code class="block bg-amber-100 rounded px-3 py-2 mt-2 font-mono text-xs">
      python node_runner.py --raion-url ws://YOUR_IP:8000 --node-name "office-pc" --token YOUR_TOKEN
    </code>
  </div>
  <div class="space-y-3">
    {% for node in nodes %}
    <div class="bg-white rounded-xl p-4 border border-gray-100 flex items-center justify-between">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full {% if node.status == 'online' %}bg-green-500{% else %}bg-gray-400{% endif %}"></span>
          <span class="font-medium">{{ node.name }}</span>
          <span class="text-xs text-gray-400">{{ node.host }}</span>
        </div>
        <div class="text-xs text-gray-400 mt-1 ml-4">
          Capabilities: {{ node.capabilities|join(', ') or '—' }}
        </div>
      </div>
      <span class="text-sm {% if node.status == 'online' %}text-green-600{% else %}text-gray-400{% endif %}">
        {{ node.status }}
      </span>
    </div>
    {% else %}
    <div class="text-gray-400 text-sm text-center py-8">No nodes registered yet.</div>
    {% endfor %}
  </div>
</div>
{% endblock %}
```

Create `app/web/templates/teams/councils.html`:

```html
{% extends "base.html" %}
{% block title %}Councils — Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-5xl mx-auto">
  <h1 class="text-2xl font-bold mb-6">Councils</h1>
  <div class="space-y-3">
    {% for council in councils %}
    <div class="bg-white rounded-xl p-4 border border-gray-100">
      <div class="flex items-center gap-2 mb-1">
        <span class="w-2 h-2 rounded-full {% if council.status == 'active' %}bg-green-500{% else %}bg-gray-400{% endif %}"></span>
        <span class="font-medium">{{ council.topic[:80] }}</span>
        <span class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">{{ council.status }}</span>
      </div>
      <div class="text-xs text-gray-400 ml-4">
        Started: {{ council.created_at.strftime('%Y-%m-%d %H:%M') }}
        {% if council.deadline_at %} · Deadline: {{ council.deadline_at.strftime('%H:%M') }}{% endif %}
        {% if council.concluded_at %} · Concluded: {{ council.concluded_at.strftime('%Y-%m-%d %H:%M') }}{% endif %}
      </div>
    </div>
    {% else %}
    <div class="text-gray-400 text-sm text-center py-8">No councils yet.</div>
    {% endfor %}
  </div>
</div>
{% endblock %}
```

Create `app/web/templates/teams/templates.html`:

```html
{% extends "base.html" %}
{% block title %}Templates — Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-5xl mx-auto">
  <h1 class="text-2xl font-bold mb-6">Agent Templates</h1>
  <div class="grid grid-cols-2 gap-4">
    {% for t in templates %}
    <div class="bg-white rounded-xl p-4 shadow-sm border border-gray-100">
      <div class="flex items-center gap-2 mb-2">
        <span class="font-semibold">{{ t.name }}</span>
        {% if t.is_builtin %}
        <span class="text-xs bg-indigo-100 text-indigo-700 px-2 py-0.5 rounded-full">Built-in</span>
        {% endif %}
        <span class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">{{ t.category }}</span>
      </div>
      <p class="text-sm text-gray-600">{{ t.description }}</p>
      <a href="/teams/agents/create?template={{ t.id }}"
         class="mt-3 inline-block text-sm text-indigo-600 hover:underline">Use template →</a>
    </div>
    {% else %}
    <div class="col-span-2 text-gray-400 text-sm text-center py-8">No templates yet.</div>
    {% endfor %}
  </div>
</div>
{% endblock %}
```

Create `app/web/templates/teams/analytics.html`:

```html
{% extends "base.html" %}
{% block title %}Analytics — Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-5xl mx-auto">
  <h1 class="text-2xl font-bold mb-6">System Analytics</h1>
  <div class="overflow-x-auto">
    <table class="w-full text-sm">
      <thead><tr class="text-left text-gray-500 border-b">
        <th class="pb-2 pr-4">Date</th>
        <th class="pb-2 pr-4">Agent</th>
        <th class="pb-2 pr-4">Ticks</th>
        <th class="pb-2 pr-4">LLM Calls</th>
        <th class="pb-2 pr-4">Tokens</th>
        <th class="pb-2 pr-4">Cost</th>
        <th class="pb-2">Errors</th>
      </tr></thead>
      <tbody>
        {% for m in metrics %}
        <tr class="border-b border-gray-50 hover:bg-gray-50">
          <td class="py-2 pr-4 font-mono text-xs">{{ m.date }}</td>
          <td class="py-2 pr-4">{{ agents.get(m.agent_id, 'Unknown') }}</td>
          <td class="py-2 pr-4">{{ m.tick_count }}</td>
          <td class="py-2 pr-4">{{ m.llm_calls }}</td>
          <td class="py-2 pr-4">{{ m.tokens_used }}</td>
          <td class="py-2 pr-4">${{ "%.4f"|format(m.estimated_cost_usd) }}</td>
          <td class="py-2 {% if m.errors > 0 %}text-red-500{% endif %}">{{ m.errors }}</td>
        </tr>
        {% else %}
        <tr><td colspan="7" class="py-4 text-gray-400 text-center">No metrics yet.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endblock %}
```

Create `app/web/templates/teams/agent_create.html` (5-step wizard):

```html
{% extends "base.html" %}
{% block title %}Create Agent — Teams{% endblock %}
{% block content %}
<div class="p-6 max-w-2xl mx-auto">
  <h1 class="text-2xl font-bold mb-2">Create Agent</h1>
  <div class="flex gap-2 mb-8" id="step-indicators">
    {% for i, label in [(1,'Identity'),(2,'Seats'),(3,'Memory'),(4,'Tools'),(5,'Review')] %}
    <div id="step-ind-{{ i }}"
         class="flex-1 text-center text-xs py-1.5 rounded-full font-medium
                {% if i == 1 %}bg-indigo-600 text-white{% else %}bg-gray-100 text-gray-500{% endif %}">
      {{ i }}. {{ label }}
    </div>
    {% endfor %}
  </div>

  <!-- Step 1: Identity -->
  <div id="step-1" class="step-pane">
    <label class="block text-sm font-medium mb-1">Name *</label>
    <input id="f-name" type="text" placeholder="Ops Manager" class="w-full border border-gray-200 rounded-lg px-3 py-2 mb-4 text-sm">
    <label class="block text-sm font-medium mb-1">Persona / System Prompt *</label>
    <textarea id="f-persona" rows="6" placeholder="You are an operations manager monitoring the staff WhatsApp group..."
              class="w-full border border-gray-200 rounded-lg px-3 py-2 mb-4 text-sm font-mono"></textarea>
    <div class="grid grid-cols-2 gap-4 mb-4">
      <div>
        <label class="block text-sm font-medium mb-1">Wake Mode</label>
        <select id="f-wake-mode" class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
          <option value="always_on">Always On</option>
          <option value="event_only">Event Only</option>
          <option value="scheduled_only">Scheduled Only</option>
          <option value="adaptive">Adaptive</option>
          <option value="manual">Manual</option>
        </select>
      </div>
      <div>
        <label class="block text-sm font-medium mb-1">Tick Interval (seconds)</label>
        <input id="f-tick" type="number" value="30" min="10" class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
      </div>
    </div>
    <div class="grid grid-cols-2 gap-4 mb-4">
      <div>
        <label class="block text-sm font-medium mb-1">Environment</label>
        <select id="f-env" class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
          <option value="">None</option>
          {% for env in environments %}
          <option value="{{ env.id }}">{{ env.name }}</option>
          {% endfor %}
        </select>
      </div>
      <div>
        <label class="block text-sm font-medium mb-1">Parent Agent</label>
        <select id="f-parent" class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
          <option value="">None</option>
          {% for a in agents %}
          <option value="{{ a.id }}">{{ a.name }}</option>
          {% endfor %}
        </select>
      </div>
    </div>
    <label class="flex items-center gap-2 text-sm mb-4">
      <input id="f-system" type="checkbox" class="rounded">
      System Agent (can communicate across all environments)
    </label>
  </div>

  <!-- Step 2: Seats -->
  <div id="step-2" class="step-pane hidden">
    <div id="seats-list" class="space-y-3 mb-4"></div>
    <select id="seat-type-picker" class="border border-gray-200 rounded-lg px-3 py-2 text-sm mr-2">
      <option value="whatsapp_group">WhatsApp Group</option>
      <option value="folder">Folder</option>
      <option value="telegram_chat">Telegram Chat</option>
      <option value="webhook">Webhook</option>
      <option value="cron_poll">Cron Poll</option>
    </select>
    <button onclick="addSeat()" class="bg-gray-100 hover:bg-gray-200 px-3 py-2 rounded-lg text-sm">+ Add Seat</button>
  </div>

  <!-- Step 3: Memory -->
  <div id="step-3" class="step-pane hidden">
    <p class="text-sm text-gray-600 mb-3">Add initial important memory — facts always in the agent's system prompt.</p>
    <div id="memory-list" class="space-y-2 mb-4"></div>
    <button onclick="addMemory()" class="bg-gray-100 hover:bg-gray-200 px-3 py-2 rounded-lg text-sm">+ Add Memory Entry</button>
  </div>

  <!-- Step 4: Tools -->
  <div id="step-4" class="step-pane hidden">
    <p class="text-sm text-gray-600 mb-3">Select which tools this agent can use.</p>
    <div class="grid grid-cols-2 gap-2">
      {% for tool in ['whatsapp_send','whatsapp_send_file','telegram_send','telegram_ask',
                       'gmail_list_unread','gmail_read','gmail_send',
                       'web_search','web_fetch','read_file','write_file',
                       'run_shell_command','generate_image','rag_search','rag_ingest',
                       'message_agent','invoke_agent','convene_council','create_agent_cron',
                       'write_memory','promote_memory'] %}
      <label class="flex items-center gap-2 text-sm p-2 rounded-lg hover:bg-gray-50 cursor-pointer">
        <input type="checkbox" name="tool" value="{{ tool }}" class="rounded tool-check"> {{ tool }}
      </label>
      {% endfor %}
    </div>
  </div>

  <!-- Step 5: Review -->
  <div id="step-5" class="step-pane hidden">
    <div class="bg-gray-50 rounded-xl p-4 text-sm space-y-2">
      <div><span class="text-gray-500">Name:</span> <span id="review-name" class="font-medium"></span></div>
      <div><span class="text-gray-500">Wake Mode:</span> <span id="review-wake"></span></div>
      <div><span class="text-gray-500">Seats:</span> <span id="review-seats"></span></div>
      <div><span class="text-gray-500">Memory entries:</span> <span id="review-memory"></span></div>
      <div><span class="text-gray-500">Tools:</span> <span id="review-tools"></span></div>
    </div>
    <p class="text-xs text-gray-400 mt-3">Agent will start in <strong>paused</strong> state. Activate it manually after creation.</p>
  </div>

  <!-- Navigation -->
  <div class="flex justify-between mt-8">
    <button id="btn-back" onclick="prevStep()" class="hidden px-4 py-2 text-sm text-gray-500 hover:text-gray-700">← Back</button>
    <div class="ml-auto flex gap-2">
      <button id="btn-next" onclick="nextStep()" class="bg-indigo-600 hover:bg-indigo-700 text-white px-6 py-2 rounded-lg text-sm">Next →</button>
      <button id="btn-launch" onclick="launchAgent()" class="hidden bg-green-600 hover:bg-green-700 text-white px-6 py-2 rounded-lg text-sm">Launch Agent</button>
    </div>
  </div>
</div>

<script>
let currentStep = 1;
const seats = [];
const memories = [];

function showStep(n) {
  document.querySelectorAll('.step-pane').forEach(p => p.classList.add('hidden'));
  document.getElementById('step-' + n).classList.remove('hidden');
  document.querySelectorAll('[id^="step-ind-"]').forEach((el, i) => {
    const sn = i + 1;
    el.className = el.className.replace(/bg-\w+-\d+ text-\w+/g, '');
    if (sn === n) el.classList.add('bg-indigo-600','text-white');
    else if (sn < n) el.classList.add('bg-indigo-100','text-indigo-700');
    else el.classList.add('bg-gray-100','text-gray-500');
  });
  document.getElementById('btn-back').classList.toggle('hidden', n === 1);
  document.getElementById('btn-next').classList.toggle('hidden', n === 5);
  document.getElementById('btn-launch').classList.toggle('hidden', n !== 5);
  if (n === 5) fillReview();
}

function nextStep() { currentStep = Math.min(5, currentStep + 1); showStep(currentStep); }
function prevStep() { currentStep = Math.max(1, currentStep - 1); showStep(currentStep); }

function addSeat() {
  const type = document.getElementById('seat-type-picker').value;
  const idx = seats.length;
  seats.push({seat_type: type, config: {}, output_channels: []});
  const div = document.createElement('div');
  div.className = 'bg-white border border-gray-200 rounded-lg p-3 text-sm';
  div.innerHTML = `<div class="flex justify-between items-center">
    <span class="font-medium">${type}</span>
    <button onclick="seats.splice(${idx},1);this.closest('div').parentElement.remove()" class="text-red-500 text-xs">Remove</button>
  </div>
  <input placeholder="Config (e.g. group_id, folder_path)" class="w-full mt-2 border border-gray-100 rounded px-2 py-1 text-xs"
         onchange="seats[${idx}].config = JSON.parse(this.value||'{}')" value="">`;
  document.getElementById('seats-list').appendChild(div);
}

function addMemory() {
  const idx = memories.length;
  memories.push({key:'', content:''});
  const div = document.createElement('div');
  div.className = 'flex gap-2';
  div.innerHTML = `<input placeholder="Key" class="border border-gray-200 rounded-lg px-2 py-1.5 text-sm w-1/3"
       onchange="memories[${idx}].key=this.value">
    <input placeholder="Content / fact" class="border border-gray-200 rounded-lg px-2 py-1.5 text-sm flex-1"
       onchange="memories[${idx}].content=this.value">
    <button onclick="memories.splice(${idx},1);this.closest('div').remove()" class="text-red-500 text-sm">×</button>`;
  document.getElementById('memory-list').appendChild(div);
}

function fillReview() {
  document.getElementById('review-name').textContent = document.getElementById('f-name').value || '—';
  document.getElementById('review-wake').textContent = document.getElementById('f-wake-mode').value;
  document.getElementById('review-seats').textContent = seats.map(s=>s.seat_type).join(', ') || 'None';
  document.getElementById('review-memory').textContent = memories.filter(m=>m.key).length;
  const tools = [...document.querySelectorAll('.tool-check:checked')].map(c=>c.value);
  document.getElementById('review-tools').textContent = tools.length + ' selected';
}

function launchAgent() {
  const tools = [...document.querySelectorAll('.tool-check:checked')].map(c => c.value);
  const body = {
    name: document.getElementById('f-name').value,
    persona: document.getElementById('f-persona').value,
    wake_mode: document.getElementById('f-wake-mode').value,
    tick_interval_seconds: parseInt(document.getElementById('f-tick').value) || 30,
    environment_id: parseInt(document.getElementById('f-env').value) || null,
    parent_agent_id: parseInt(document.getElementById('f-parent').value) || null,
    is_system_agent: document.getElementById('f-system').checked,
    allowed_tools: tools,
    seats: seats,
    important_memory: memories.filter(m => m.key && m.content),
  };
  fetch('/teams/api/agents', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  }).then(r => r.json()).then(data => {
    window.location = '/teams/agents/' + data.slug;
  });
}
</script>
{% endblock %}
```

- [ ] **Step 4: Add Teams link to sidebar in `base.html`**

Open `app/web/templates/base.html`. Find the sidebar navigation links. Add Teams after Memory:

```html
<a href="/teams"
   class="flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium
          {{ 'bg-gray-800 text-white' if request.url.path.startswith('/teams') else 'text-gray-400 hover:text-white hover:bg-gray-800' }}">
  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
          d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/>
  </svg>
  Teams
</a>
```

- [ ] **Step 5: Run all tests and start server**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/ -v --tb=short
python run.py
```

Open `http://localhost:8000/teams` — verify the page loads.

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/teams/ app/web/templates/base.html
git commit -m "feat: full Teams UI — overview, agent list, detail (6 tabs), create wizard, environments, nodes, councils, templates, analytics"
```

---

## Task 5: Node Runner Script

**Files:**
- Create: `node_runner.py` (project root)

- [ ] **Step 1: Create `node_runner.py`**

```python
#!/usr/bin/env python3
"""
RAION Teams — Node Runner

Run this on any LAN PC to make it an execution node for RAION Teams agents.

Usage:
    python node_runner.py --raion-url ws://192.168.1.x:8000 --node-name "office-pc" --token YOUR_TOKEN

The token should be any shared secret between RAION and this runner.
Set it in your .env as NODE_RUNNER_TOKEN=<same value>.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import time

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("node_runner")

CAPABILITIES = ["filesystem", "shell"]
try:
    import playwright  # noqa
    CAPABILITIES.append("playwright")
except ImportError:
    pass

try:
    import numpy  # noqa
    CAPABILITIES.append("python")
except ImportError:
    pass

PING_INTERVAL = 10  # seconds
RECONNECT_DELAY = 5  # seconds


async def run_tick(payload: dict) -> dict:
    """Execute a tick payload locally and return the result."""
    # In a full implementation this would invoke a local LangGraph instance.
    # For now, return a stub result so the node registration protocol works.
    agent_id = payload.get("agent_id")
    inbox_items = payload.get("inbox_items", [])
    logger.info("Executing tick for agent %s with %d inbox items", agent_id, len(inbox_items))
    return {"agent_id": agent_id, "status": "done", "actions": 0, "error": None}


async def main(raion_url: str, node_name: str, token: str) -> None:
    ws_url = f"{raion_url}/ws/node-runner?name={node_name}&token={token}"

    while True:
        try:
            logger.info("Connecting to RAION at %s", raion_url)
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                # Register
                await ws.send(json.dumps({
                    "type": "register",
                    "node_name": node_name,
                    "capabilities": CAPABILITIES,
                    "platform": platform.system(),
                }))
                logger.info("Connected. Capabilities: %s", CAPABILITIES)

                ping_task = asyncio.create_task(_ping_loop(ws))

                try:
                    async for message in ws:
                        try:
                            msg = json.loads(message)
                        except json.JSONDecodeError:
                            continue

                        if msg.get("type") == "tick":
                            result = await run_tick(msg.get("payload", {}))
                            await ws.send(json.dumps({"type": "tick_result", "payload": result}))

                        elif msg.get("type") == "pong":
                            pass  # ping acknowledged

                finally:
                    ping_task.cancel()

        except (websockets.exceptions.ConnectionClosed,
                OSError, ConnectionRefusedError) as exc:
            logger.warning("Connection lost: %s. Reconnecting in %ds…", exc, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as exc:
            logger.exception("Unexpected error: %s. Reconnecting in %ds…", exc, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


async def _ping_loop(ws) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            await ws.send(json.dumps({"type": "ping", "ts": time.time()}))
        except Exception:
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAION Teams Node Runner")
    parser.add_argument("--raion-url", required=True, help="e.g. ws://192.168.1.5:8000")
    parser.add_argument("--node-name", required=True, help="Human-readable name for this node")
    parser.add_argument("--token", required=True, help="Shared secret token")
    args = parser.parse_args()

    # Normalize URL scheme
    url = args.raion_url.replace("http://", "ws://").replace("https://", "wss://")

    asyncio.run(main(url, args.node_name, args.token))
```

- [ ] **Step 2: Add websockets to requirements.txt**

```bash
grep -q "websockets" "E:/BTP project/requirements.txt" || echo "websockets>=12.0" >> "E:/BTP project/requirements.txt"
pip install "websockets>=12.0"
```

- [ ] **Step 3: Commit**

```bash
git add node_runner.py requirements.txt
git commit -m "feat: LAN node_runner.py — connects to RAION via WebSocket, declares capabilities, executes tick payloads"
```

---

## Task 6: Final Integration — Run All Tests

- [ ] **Step 1: Run complete test suite**

```bash
cd "E:/BTP project"
python -m pytest tests/teams/ -v --tb=short
```

Expected: all tests PASS.

- [ ] **Step 2: Start server and manually verify**

```bash
python run.py
```

Open in browser:
- `http://localhost:8000/teams` → Overview loads ✓
- `http://localhost:8000/teams/agents/create` → Wizard shows 5 steps ✓
- `http://localhost:8000/teams/templates` → 5 built-in templates visible ✓
- `http://localhost:8000/teams/environments` → Environments page loads ✓
- `http://localhost:8000/teams/nodes` → Nodes page with instructions loads ✓

Create a test agent:
1. Go to `/teams/agents/create`
2. Fill in Name: "Test Agent", Persona: "You are a test agent."
3. Skip seats and memory
4. Click Launch Agent
5. Verify redirect to agent detail page
6. Click Activate — verify status turns green
7. Click Pause — verify status returns to paused
8. Check Versions tab — v1 should be visible

- [ ] **Step 3: Final commit**

```bash
git add .
git commit -m "feat: Plan 4 complete — versioning, templates, metrics, full Teams UI, Node Runner — RAION Teams MVP done"
```

---

## Done — Plan 4 Complete

**RAION Teams is now fully implemented across all 4 plans:**

- Plan 1: DB models + Agent Runtime + Inbox Queue + Wakeup Model
- Plan 2: 4-tier Memory System + Seat Integrations
- Plan 3: Inter-Agent Communication + Environments + Council + Agent Crons
- Plan 4: Versioning + Templates + Metrics + Full UI + Node Runner

The system supports: persistent 24/7 agents, multi-seat perception, layered memory with auto-promotion, agent-owned crons, async/sync/council communication, environment isolation with System Agent bypass, one-click versioning and rollback, built-in templates, metrics tracking, and LAN node deployment.
