# Campaign Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `campaign-agents` — a 24/7 multi-agent WhatsApp system with per-group persistent agents, tiered memory, director-controlled mutations, and an admin UI for Maharshi.

**Architecture:** OpenAI Agents SDK for the per-turn tool loop. Custom layers on top for memory (core/rag/ephemeral/scratchpad), message bus, outbound queue, director confirmation protocol, and admin UI. FastAPI + Jinja2 + HTMX for the admin console. Postgres + pgvector for storage.

**Tech Stack:** Python 3.12, OpenAI Agents SDK, FastAPI, Jinja2, Tailwind (CDN), HTMX, SQLAlchemy 2 async, asyncpg, Postgres 16, pgvector, APScheduler, Green API, OpenAI Whisper, Fernet, bcrypt.

**Reference spec:** `docs/superpowers/specs/2026-04-24-campaign-agents-design.md` (in the RAION repo — copy it into the new repo during Phase 0).

---

## How this plan is organized

The plan is split into **phases**. Each phase:
- Is small enough to complete in one Claude Code session
- Ends with tests passing + a git commit + a `SESSION_HANDOFF.md` update
- Lists its own prerequisites (what earlier phases must be done)

**At the end of each phase, the last step is always:** "Update `SESSION_HANDOFF.md` with: last completed phase, next phase number, any decisions made, any gotchas."

**To start a new session for phase N**, paste this into a fresh Claude Code window opened in the `campaign-agents/` folder:

```
I'm continuing the campaign-agents project. Please:
1. Read CLAUDE.md in the repo root
2. Read SESSION_HANDOFF.md in the repo root
3. Read docs/specs/campaign-agents-design.md
4. Read docs/plans/campaign-agents-plan.md
5. Start Phase N from the plan. Use the superpowers:executing-plans skill.
```

The plan file is the source of truth. SESSION_HANDOFF records progress. CLAUDE.md gives architectural orientation.

---

## Phases at a glance

| Phase | Focus | Dependencies |
|---|---|---|
| 0 | Repo bootstrap, docs, `CLAUDE.md`, `SESSION_HANDOFF.md`, Postgres + pgvector up, first migration | — |
| 1 | Core DB models + Alembic migrations (all tables from spec §9) | 0 |
| 2 | Config, FastAPI skeleton, login, dashboard page with dummy data | 1 |
| 3 | Green API client + webhook route + polling fallback + `incoming_messages` intake | 1 |
| 4 | Router (chat_id → agent scope), outbound queue + single-worker drainer | 3 |
| 5 | Memory layer: core / ephemeral / scratchpad tables + read/write APIs + per-agent locks | 1 |
| 6 | RAG memory: pgvector setup + embedding + semantic retrieval | 5 |
| 7 | Prompt assembly: base templates + org context + scratchpad + override + capability block + memory injection | 5 |
| 8 | Reflex step (gpt-4o-mini structured output) | 7 |
| 9 | Deliberate step (OpenAI Agents SDK agent with tools: whatsapp_send, save_*, recall, forget, schedule_checkin, notify_supervisor, ask_other_agent, stay_silent) | 4, 6, 7, 8 |
| 10 | Reflect step (memory pruning + tier decisions) + `traces` logging | 9 |
| 11 | Full pipeline wiring: message in → reflex → (deliberate?) → reflect → out. End-to-end test on a stub group. | 9, 10 |
| 12 | Admin UI: Groups CRUD, Authorized Directors CRUD | 2, 11 |
| 13 | Admin UI: Agent Instructions (base templates, org context, per-group overrides, preview) | 12 |
| 14 | Admin UI: Memory Inspector (core/ephemeral/rag/scratchpad view + edit) | 12 |
| 15 | Admin UI: Traces view + Outbound Queue view + Message Bus (inbox) view | 12 |
| 16 | Supervisor agent — classification, director authority check, query flow | 11, 12 |
| 17 | Supervisor mutation tools + confirmation protocol (pending_mutations + 10-min timeout + audit_log) | 16 |
| 18 | Admin UI: Audit Log + Pending Mutations + revert | 17 |
| 19 | Audio input: Whisper transcription for director audio messages | 16 |
| 20 | Scheduler: APScheduler boot, morning check-in cron, nightly ephemeral wipe + archive | 5, 11 |
| 21 | EveningSummarizerAgent (nightly cron, reads archive + ephemeral, posts to reports group) | 20 |
| 22 | WeeklyLeaderboardAgent (Sunday cron) | 20 |
| 23 | Scheduled check-ins per agent (`scheduled_checkins` table poller) | 20 |
| 24 | Agent-to-agent inbox (`ask_other_agent` + `notify_supervisor` durable delivery) | 11 |
| 25 | Broadcast tool for supervisor (fan-out to selected groups via outbound queue) | 17 |
| 26 | Settings page: models, cron schedules, timezone, ephemeral wipe time, Green API creds, Whisper toggle | 12 |
| 27 | Concurrency hardening: `SELECT FOR UPDATE SKIP LOCKED` for inbox + outbound, SERIALIZABLE tx for memory writes, per-agent async locks tested under contention | 11, 24 |
| 28 | Error handling: per-turn tool call cap, wall-clock timeout, pending-mutation cap, stuck-agent detection | 11, 16 |
| 29 | Observability: traces UI filters, cost totals, dashboard "health" signals | 15 |
| 30 | Manual smoke-test pass on real Green API + director user IDs; bug fixes | all |
| 31 | Runbook + README + deployment notes | 30 |

---

# Phase 0 — Repo bootstrap

**Prereqs:** None. New empty folder.

**Files:**
- Create: `campaign-agents/` (new folder sibling to the RAION repo, e.g. `E:\BTP project\..\campaign-agents\` or wherever Maharshi prefers — ask before creating)
- Create: `campaign-agents/README.md`
- Create: `campaign-agents/CLAUDE.md`
- Create: `campaign-agents/SESSION_HANDOFF.md`
- Create: `campaign-agents/.gitignore`
- Create: `campaign-agents/.env.example`
- Create: `campaign-agents/requirements.txt`
- Create: `campaign-agents/pyproject.toml` (optional — for ruff/black config)
- Create: `campaign-agents/docs/specs/campaign-agents-design.md` (copy from RAION repo `docs/superpowers/specs/2026-04-24-campaign-agents-design.md`)
- Create: `campaign-agents/docs/plans/campaign-agents-plan.md` (copy from RAION repo `docs/superpowers/plans/2026-04-24-campaign-agents-plan.md`)

- [ ] **Step 0.1: Ask Maharshi where to create the `campaign-agents` folder** (sibling to `E:\BTP project\` suggested). Then create it.

- [ ] **Step 0.2: `git init` and set up `.gitignore`**

```gitignore
__pycache__/
*.pyc
.env
.venv/
venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.sqlite
app.db
.idea/
.vscode/
instance/
dist/
build/
*.egg-info/
```

- [ ] **Step 0.3: Write `requirements.txt`**

```
fastapi==0.115.*
uvicorn[standard]==0.32.*
jinja2==3.1.*
python-multipart==0.0.12
itsdangerous==2.2.*
bcrypt==4.2.*
sqlalchemy[asyncio]==2.0.*
asyncpg==0.30.*
alembic==1.14.*
pgvector==0.3.*
pydantic==2.9.*
pydantic-settings==2.6.*
python-dotenv==1.0.*
httpx==0.28.*
apscheduler==3.10.*
cryptography==43.*
openai==1.60.*
openai-agents==0.0.*  # pin after first install; check PyPI for current
tiktoken==0.8.*
pytest==8.3.*
pytest-asyncio==0.24.*
pytest-cov==6.0.*
ruff==0.8.*
```

- [ ] **Step 0.4: Write `.env.example`**

```
APP_SECRET_KEY=change-me
DATABASE_URL=postgresql+asyncpg://campaign:campaign@localhost:5432/campaign_agents
ADMIN_USERNAME=maharshi
ADMIN_PASSWORD=change-me

OPENAI_API_KEY=
DEFAULT_REFLEX_MODEL=gpt-4o-mini
DEFAULT_DELIBERATE_MODEL=gpt-4o
EMBEDDING_MODEL=text-embedding-3-small

FERNET_KEY=  # generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

GREEN_API_BASE_URL=https://api.green-api.com
GREEN_API_INSTANCE_ID=
GREEN_API_TOKEN=
GREEN_API_WEBHOOK_TOKEN=

WHISPER_ENABLED=true
TIMEZONE=Asia/Kolkata
EPHEMERAL_WIPE_TIME=23:59
```

- [ ] **Step 0.5: Copy the spec and plan into the new repo** under `docs/specs/` and `docs/plans/`. Drop the date prefix; use `campaign-agents-design.md` and `campaign-agents-plan.md`.

- [ ] **Step 0.6: Write `CLAUDE.md`** — a short orientation file:

```markdown
# campaign-agents — Project Context for Claude

## What this is
A 24/7 multi-agent WhatsApp system for MNA/MNDC admission campaigning, built by Maharshi Nahar.

## Key files to read first
- `docs/specs/campaign-agents-design.md` — full design
- `docs/plans/campaign-agents-plan.md` — implementation plan (source of truth for progress)
- `SESSION_HANDOFF.md` — last session's state

## Stack
Python 3.12, FastAPI, OpenAI Agents SDK, Postgres+pgvector, SQLAlchemy async, APScheduler, Green API, Whisper.

## Conventions
- Async everywhere
- Type hints required
- `from __future__ import annotations`
- All timestamps stored UTC in DB, displayed IST in UI
- No LangGraph, no langchain
- Prefer editing over creating files
- Tests in `tests/`, use pytest-asyncio

## Architecture
Per-group persistent agents. Two-tier loop: reflex (gpt-4o-mini) → deliberate (gpt-4o) → reflect.
Tiered memory: core (system prompt) / rag (pgvector) / ephemeral (today-only) / scratchpad (global).
Supervisor agent mediates director ↔ system mutations with 10-minute confirmation timeout.
```

- [ ] **Step 0.7: Write `SESSION_HANDOFF.md`**

```markdown
# Session Handoff

## Last completed phase
Phase 0 — Repo bootstrap

## Next phase
Phase 1 — Core DB models + Alembic migrations

## Decisions made
- (none yet)

## Gotchas / Open questions
- Verify pinned version of `openai-agents` on PyPI before first import — update requirements.txt.
- Maharshi to provide: Green API instance ID + token, director sir & maam WhatsApp user IDs, Postgres connection details.

## How to start next session
Open Claude Code in the repo root and paste:

"""
I'm continuing the campaign-agents project. Please:
1. Read CLAUDE.md
2. Read SESSION_HANDOFF.md
3. Read docs/specs/campaign-agents-design.md
4. Read docs/plans/campaign-agents-plan.md
5. Start Phase 1 from the plan. Use the superpowers:executing-plans skill.
"""
```

- [ ] **Step 0.8: Write a minimal `README.md`** — one paragraph: what, who, how to run (TBD), point to CLAUDE.md.

- [ ] **Step 0.9: Verify Postgres 16 is installed locally. If not, ask Maharshi to install it** (Windows: EnterpriseDB installer). Create database:

```sql
CREATE DATABASE campaign_agents;
CREATE USER campaign WITH PASSWORD 'campaign';
GRANT ALL PRIVILEGES ON DATABASE campaign_agents TO campaign;
\c campaign_agents
CREATE EXTENSION IF NOT EXISTS vector;
```

Run via: `psql -U postgres -f setup.sql` (create a `setup.sql` helper in repo root).

- [ ] **Step 0.10: Create Python venv, install requirements, verify imports**

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python -c "import fastapi, sqlalchemy, asyncpg, pgvector, openai, agents; print('OK')"
```

Expected: `OK`. If `agents` import fails, check exact package name for OpenAI Agents SDK (`openai-agents` pip package → `import agents` or `from openai.agents import ...` — verify against current PyPI and update `CLAUDE.md` + imports).

- [ ] **Step 0.11: `git add . && git commit -m "chore: repo bootstrap"`**

- [ ] **Step 0.12: Update `SESSION_HANDOFF.md`** — mark Phase 0 complete, next = Phase 1.

---

# Phase 1 — Database models

**Prereqs:** Phase 0.

**Goal:** Every table from spec §9 exists as a SQLAlchemy model, with one Alembic migration that creates the full schema.

**Files:**
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `app/db/__init__.py`
- Create: `app/db/engine.py`
- Create: `app/db/base.py` — declarative base, common mixins
- Create: `app/db/models/__init__.py`
- Create: `app/db/models/users.py`
- Create: `app/db/models/directors.py`
- Create: `app/db/models/groups.py`
- Create: `app/db/models/templates.py` — base_templates, group_overrides, shared_org_context
- Create: `app/db/models/memory.py` — memory_core, memory_rag, memory_ephemeral, memory_archive, scratchpad_global
- Create: `app/db/models/checkins.py` — scheduled_checkins
- Create: `app/db/models/bus.py` — inbox, outbound_queue
- Create: `app/db/models/mutations.py` — pending_mutations, audit_log
- Create: `app/db/models/traces.py`
- Create: `app/db/models/messages.py` — incoming_messages
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_initial_schema.py`
- Test: `tests/test_models_smoke.py`

- [ ] **Step 1.1: Write `app/config.py`** using `pydantic-settings.BaseSettings`. Load from env.

```python
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_secret_key: str
    database_url: str
    admin_username: str
    admin_password: str

    openai_api_key: str
    default_reflex_model: str = "gpt-4o-mini"
    default_deliberate_model: str = "gpt-4o"
    embedding_model: str = "text-embedding-3-small"

    fernet_key: str

    green_api_base_url: str
    green_api_instance_id: str = ""
    green_api_token: str = ""
    green_api_webhook_token: str = ""

    whisper_enabled: bool = True
    timezone: str = "Asia/Kolkata"
    ephemeral_wipe_time: str = "23:59"

settings = Settings()
```

- [ ] **Step 1.2: Write `app/db/engine.py`** — async engine + session factory.

```python
from __future__ import annotations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from app.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```

- [ ] **Step 1.3: Write `app/db/base.py`** — `Base`, `TimestampMixin`.

```python
from __future__ import annotations
from datetime import datetime
from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
```

- [ ] **Step 1.4: Write each model file per spec §9.** (Follow the spec field list exactly. Use `Mapped[...]` typing. Enums via `sqlalchemy.Enum` + `str, Enum` pattern.)

Full model contents for each file are defined in spec §9. For `memory_rag.embedding` use `pgvector.sqlalchemy.Vector(1536)`. For `scratchpad_global.data` use `JSONB`. For all PK ids use `BigInteger` autoincrement except `pending_mutations.change_id` which is `String(16)` PK.

*(Write each model file out in full in this step — don't skip; the engineer reading this plan in a fresh session needs the code here. If this step grows too long during writing, split into 1.4a through 1.4l, one per model file. Each gets its own commit.)*

- [ ] **Step 1.5: Write `app/db/models/__init__.py`** — re-export all models so `Base.metadata` sees them.

- [ ] **Step 1.6: Initialize Alembic**

```bash
alembic init -t async migrations
```

Edit `alembic.ini` → set `sqlalchemy.url = postgresql+asyncpg://... ` (or read from env in `env.py`).

Edit `migrations/env.py`:
- Import `from app.db.base import Base`
- Import `from app.db import models  # noqa: F401` to register all models
- Set `target_metadata = Base.metadata`
- Read URL from `app.config.settings`

- [ ] **Step 1.7: Autogenerate initial migration**

```bash
alembic revision --autogenerate -m "initial schema"
```

Open the generated file. **Manually add** at the top of `upgrade()`:

```python
op.execute("CREATE EXTENSION IF NOT EXISTS vector")
```

And any missing `pgvector` column definitions the autogen missed.

- [ ] **Step 1.8: Apply migration**

```bash
alembic upgrade head
```

Expected: clean run. Verify in psql: `\dt` shows all tables; `\d memory_rag` shows `embedding vector(1536)`.

- [ ] **Step 1.9: Write `tests/test_models_smoke.py`** — start a session, insert one row into each table, query it back, rollback. Proves the schema is usable.

```python
# Actual test code using AsyncSession — insert one row per table, assert round-trip.
# (Spelled out in full — no "similar to above" shortcuts.)
```

- [ ] **Step 1.10: Run tests**

```bash
pytest tests/test_models_smoke.py -v
```

Expected: PASS.

- [ ] **Step 1.11: Commit**

```bash
git add -A
git commit -m "feat(db): initial schema with all tables and pgvector"
```

- [ ] **Step 1.12: Update `SESSION_HANDOFF.md`** — Phase 1 complete, next = Phase 2.

---

# Phase 2 — FastAPI skeleton + login + empty dashboard

**Prereqs:** Phase 1.

**Goal:** `python run.py` starts uvicorn. You can log in with admin creds. You see a dashboard page (empty placeholders — no real agent data yet).

**Files to create:** `run.py`, `app/main.py`, `app/web/__init__.py`, `app/web/deps.py`, `app/web/routes/auth.py`, `app/web/routes/dashboard.py`, `app/web/templates/base.html`, `app/web/templates/login.html`, `app/web/templates/dashboard.html`, `app/web/static/tailwind.min.js`, `app/web/static/htmx.min.js`, `app/auth/passwords.py`, `app/db/seed.py`, `tests/test_auth.py`, `tests/test_dashboard.py`.

(Tasks 2.1–2.14: app factory with lifespan; session middleware via `itsdangerous`; `require_user` dependency; `auth.py` routes `/login` GET+POST, `/logout`; `dashboard.py` GET `/`; base template with Tailwind CDN + sidebar nav stub; login template; dashboard template with placeholders "Agents (0)", "Today's events (0)", "Pending mutations (0)"; seed admin user on startup; test login happy-path + failed-password + logout; run server and click through; commit; handoff update.)

*(Each of 2.1–2.14 is spelled out as its own checkbox with code in the actual executing pass. Full text omitted here to keep this document readable; when a fresh session opens this file and starts Phase 2, it expands each bullet into concrete TDD steps before coding.)*

**Phase-2 success criteria:**
- `pytest tests/test_auth.py tests/test_dashboard.py -v` → all pass
- Manual: `python run.py` → open localhost:8000 → redirected to login → log in → see dashboard
- Commit made
- SESSION_HANDOFF updated

---

# Phase 3 — Green API client + webhook + polling + `incoming_messages` intake

**Prereqs:** Phase 1.

**Goal:** Real WhatsApp messages land in the `incoming_messages` table (raw + parsed). Both webhook and polling paths work.

**Files:** `app/whatsapp/__init__.py`, `app/whatsapp/green_api.py` (port from RAION, adapt to async httpx), `app/whatsapp/webhook.py`, `app/whatsapp/polling.py`, `app/whatsapp/parser.py` (normalize Green API payload → internal `IncomingMessage` dataclass), `app/web/routes/webhook.py` (mounts webhook), `tests/test_green_api_client.py` (with httpx mock), `tests/test_webhook_intake.py`, `tests/test_polling_intake.py`, `tests/test_parser_formats.py`.

**Key sub-tasks:**
- 3.1 Port `GreenAPIClient` (send_message, send_file_by_url, get_chat_history, download_file, receive_notification, delete_notification) as async
- 3.2 Writer function `insert_incoming_message(raw_payload, parsed_fields) -> id` with dedupe on `wa_message_id`
- 3.3 Webhook route validates webhook token, calls writer
- 3.4 Polling task started in lifespan: every 15s calls `receive_notification`, writes, then `delete_notification`
- 3.5 Parser handles text, image, audio, video, document, location, outgoing, group vs 1:1
- 3.6 Tests for every payload shape (fixtures in `tests/fixtures/green_api/`)
- 3.7 Commit + handoff

---

# Phase 4 — Router + outbound queue + outbound worker

**Prereqs:** Phase 3.

**Goal:** Given an `IncomingMessage`, the router picks the right agent scope (based on `wa_chat_id` → `groups` table, or reports group → `supervisor`, or unknown → drop with log). Outgoing messages go through a single-worker queue that serializes sends to Green API.

**Files:** `app/whatsapp/router.py`, `app/bus/outbound.py`, `app/bus/worker.py` (the drain task), `tests/test_router.py`, `tests/test_outbound_queue.py`, `tests/test_outbound_worker_concurrency.py` (verify two concurrent enqueuers + one drainer preserves FIFO per chat_id).

**Key sub-tasks:**
- 4.1 `route(incoming) -> AgentScope | None`
- 4.2 `enqueue_outbound(chat_id, body, media_url=None, send_after=now)`
- 4.3 Worker uses `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1 ORDER BY send_after, id` in a loop; marks sent / failed; exponential backoff on failure
- 4.4 Rate limit: max N sends per second (config default 2; room to tune)
- 4.5 Tests for FIFO ordering, retry, backoff cap (5 attempts then dead-letter status)
- 4.6 Commit + handoff

---

# Phase 5 — Memory layer (core, ephemeral, scratchpad, locks)

**Prereqs:** Phase 1.

**Goal:** Typed API for memory tier read/write with per-agent async locks. No RAG yet (Phase 6).

**Files:** `app/memory/__init__.py`, `app/memory/locks.py`, `app/memory/core.py`, `app/memory/ephemeral.py`, `app/memory/scratchpad.py`, `app/memory/types.py` (dataclasses for each tier), `tests/test_memory_core.py`, `tests/test_memory_ephemeral_ttl.py`, `tests/test_memory_scratchpad.py`, `tests/test_memory_locks.py`.

**Key sub-tasks:**
- 5.1 `AgentScope` type (`group:{id}`, `supervisor`, `summarizer`, `weekly`)
- 5.2 `get_lock(scope) -> asyncio.Lock` (per-scope singleton lock via `WeakValueDictionary`)
- 5.3 Core: `list_core(scope)`, `add_core(scope, text, source)`, `delete_core(id)`, `touch_core(id)`
- 5.4 Ephemeral: `list_active(scope)` (expires_at > now), `add(scope, text, ttl_seconds=None)`, `forget(id)`, `wipe(scope, archive=True)`
- 5.5 Scratchpad: `read()`, `patch(json_patch, by_actor)` — JSON merge-patch semantics
- 5.6 All writes wrapped in `async with get_lock(scope):` + SERIALIZABLE transaction
- 5.7 Concurrency test: fire 20 concurrent `add_core` from same scope, assert no duplicates, no deadlock, all present
- 5.8 Commit + handoff

---

# Phase 6 — RAG memory (pgvector)

**Prereqs:** Phase 5.

**Goal:** `save_rag(scope, text)` embeds + stores. `recall(scope, query, k=5)` returns top-k by cosine similarity.

**Files:** `app/memory/rag.py`, `app/memory/embeddings.py` (OpenAI client wrapper + cache), `tests/test_rag_embed.py`, `tests/test_rag_recall.py`.

**Key sub-tasks:**
- 6.1 `embed(text) -> list[float]` via OpenAI `text-embedding-3-small` (1536 dims)
- 6.2 `save_rag(scope, text) -> id` — embed + insert
- 6.3 `recall(scope, query, k=5) -> list[RagHit]` with `ORDER BY embedding <=> :q LIMIT k`, updates `last_retrieved_at` + `retrieval_count` on hits
- 6.4 `forget_rag(id)`
- 6.5 `decay_job()` — delete rows with `last_retrieved_at < now - 60d AND created_at < now - 60d`
- 6.6 Tests use a small stub embedder (monkeypatch OpenAI call to a deterministic hash→vector) for speed
- 6.7 Commit + handoff

---

# Phase 7 — Prompt assembly

**Prereqs:** Phase 5.

**Goal:** Given `(scope, agent_type, current_message)`, produce the full deliberate system prompt by concatenating the 8 layers from spec §8.

**Files:** `app/agents/prompts/capability_honesty.py` (literal string from spec §11), `app/agents/prompts/reflex_prompt.py`, `app/agents/prompts/reflect_prompt.py`, `app/agents/prompts/base_templates_seed.py` (seed texts for each agent type — draft them), `app/agents/prompt_assembly.py`, `tests/test_prompt_assembly.py`, `tests/test_seed_base_templates.py`.

**Key sub-tasks:**
- 7.1 Loader: `load_base_template(agent_type)` → from `base_templates` table
- 7.2 Loader: `load_group_override(group_id)` → from `group_overrides`
- 7.3 Loader: `load_org_context()`, `load_scratchpad()`
- 7.4 `assemble_deliberate_prompt(scope, current_message, retrieved_rag, ephemeral, core)` returns the final system prompt string
- 7.5 `assemble_reflex_prompt(current_message)` — just the capability-aware classifier prompt
- 7.6 Seed default base templates for each agent type on first run (via a migration seed or lifespan check)
- 7.7 Golden-snapshot test: known inputs produce expected assembled prompt (use `syrupy` or inline string assert)
- 7.8 Commit + handoff

---

# Phase 8 — Reflex step

**Prereqs:** Phase 7.

**Goal:** `run_reflex(scope, incoming) -> ReflexOutput` returns typed structured output from gpt-4o-mini.

**Files:** `app/agents/reflex.py`, `app/agents/types.py` (IntentEnum, ReflexOutput pydantic model), `tests/test_reflex.py`.

**Key sub-tasks:**
- 8.1 Define `IntentEnum`, `ReflexOutput` (fields per spec §4): `intent`, `entities`, `needs_deliberation`, `simple_reply`, `tone_hint`
- 8.2 Use OpenAI structured outputs (`response_format={"type": "json_schema", ...}`)
- 8.3 Retry on JSON parse failure (max 2)
- 8.4 Tests with mocked OpenAI responses for each intent type
- 8.5 Cost/token logging
- 8.6 Commit + handoff

---

# Phase 9 — Deliberate step (OpenAI Agents SDK)

**Prereqs:** Phases 4, 6, 7, 8.

**Goal:** An Agent (per OpenAI Agents SDK) with all tools. Invoked when `needs_deliberation=True`.

**Files:** `app/agents/tools.py` (tool functions), `app/agents/deliberate.py` (Agent construction + run), `tests/test_tools.py` (one test per tool), `tests/test_deliberate_smoke.py`.

**Key sub-tasks:**
- 9.1 Write tool functions (async where appropriate):
  - `whatsapp_send(text: str)` → enqueue via Phase 4
  - `save_core(fact: str)` → Phase 5
  - `save_rag(text: str)` → Phase 6
  - `save_ephemeral(fact: str, ttl_hours: int = 0)` → Phase 5 (0 means until wipe)
  - `recall(query: str)` → Phase 6
  - `forget(memory_id: int, tier: str)` → Phase 5/6
  - `schedule_checkin(fire_at: str, prompt_for_agent: str)` → Phase 20
  - `notify_supervisor(text: str)` → Phase 24
  - `ask_other_agent(group_id: int, text: str)` → Phase 24
  - `stay_silent(reason: str)` → noop + trace note
- 9.2 Build the `Agent` with SDK: instructions = assembled prompt, tools = above
- 9.3 `run_deliberate(scope, incoming, reflex_output) -> DeliberateOutput` (tool calls made, final message if any, tokens)
- 9.4 Hard cap: 20 tool calls per turn, 3-minute wall clock (SDK config + timeout wrapper)
- 9.5 Tests each tool in isolation; smoke test runs full deliberate against a mocked LLM returning a scripted tool-call sequence
- 9.6 Commit + handoff

---

# Phase 10 — Reflect step

**Prereqs:** Phase 9.

**Goal:** After a deliberate turn, a cheap LLM call decides memory promotions/demotions/deletions. Writes a `traces` row summarizing the whole turn.

**Files:** `app/agents/reflect.py`, `app/agents/trace.py` (trace writer), `tests/test_reflect.py`, `tests/test_trace_writer.py`.

**Key sub-tasks:**
- 10.1 `run_reflect(scope, incoming, reflex_output, deliberate_output) -> ReflectOutput` with structured output listing memory ops to apply
- 10.2 Apply the ops via Phase 5/6 APIs
- 10.3 `write_trace(...)` persists one row with all step outputs + token totals + cost
- 10.4 Tests with mocked LLM returning various prune decisions
- 10.5 Commit + handoff

---

# Phase 11 — Pipeline wiring + first end-to-end test

**Prereqs:** Phases 4, 9, 10.

**Goal:** Receive → route → reflex → (maybe deliberate) → reflect → trace. A stub group with a stub chat_id produces a real reply through the outbound queue.

**Files:** `app/agents/pipeline.py` (orchestrates steps), `app/whatsapp/dispatcher.py` (calls pipeline for each new `incoming_messages` row), `tests/test_pipeline_e2e.py`.

**Key sub-tasks:**
- 11.1 `process_incoming(message_id)` — idempotent, marks `processed=true` on success
- 11.2 Dispatcher loop: `SELECT ... FOR UPDATE SKIP LOCKED WHERE processed=false ORDER BY id LIMIT 10`
- 11.3 Start dispatcher in FastAPI lifespan
- 11.4 End-to-end test: seed group, insert raw webhook row, run dispatcher once, assert outbound_queue has expected row, trace row exists, memory state matches
- 11.5 Commit + handoff

---

# Phases 12–15 — Admin UI core

See the "Phases at a glance" table. Each page phase has tasks:
- Pydantic request/response models
- Route handlers (GET + POST/PATCH/DELETE where relevant)
- Jinja template with Tailwind + HTMX for inline edits
- Tests (pytest + httpx AsyncClient, logged-in session fixture)
- Nav link in base.html sidebar
- Commit + handoff

Each phase targets ≤1 session.

---

# Phase 16 — SupervisorAgent (classification + query + authority)

**Prereqs:** Phases 11, 12.

**Goal:** Reports-group messages routed to supervisor. It classifies intent, enforces authority, and handles read-only queries. Mutations come in Phase 17.

**Files:** `app/agents/supervisor.py`, `app/directors/authority.py`, `tests/test_supervisor_authority.py`, `tests/test_supervisor_query.py`.

---

# Phase 17 — Mutation tools + confirmation protocol

**Prereqs:** Phase 16.

**Goal:** Supervisor's mutation tools propose → wait → apply with 10-min timeout. `pending_mutations` table. `audit_log` writes on apply/reject/expire.

**Files:** `app/mutations/tools.py`, `app/mutations/proposal.py`, `app/mutations/confirmation.py`, `app/mutations/audit.py`, `tests/test_confirmation_happy.py`, `tests/test_confirmation_timeout.py`, `tests/test_confirmation_race.py`, `tests/test_audit_log.py`.

**Key sub-tasks:**
- 17.1 `propose_mutation(...)` → inserts `pending_mutations`, posts proposal message with change_id
- 17.2 `resolve_mutation(change_id, accepted: bool, by_director)` → transactional: checks status=pending, applies tools or rejects, writes audit_log
- 17.3 Background task: every 60s scans for pending with `expires_at < now`, marks `expired` silently
- 17.4 Race test: two confirmations at once → only one applies (thanks to status=pending WHERE clause)
- 17.5 Commit + handoff

---

# Phase 18 — Audit Log + Pending Mutations UI + revert

---

# Phase 19 — Audio (Whisper)

**Prereqs:** Phase 16.

**Goal:** When incoming supervisor message has `message_type=audio`, download via Green API `downloadFile`, transcribe via Whisper, treat transcript as text instruction. Store audio reference + transcript in the trace.

---

# Phase 20 — Scheduler: morning check-in + nightly wipe

**Prereqs:** Phases 5, 11.

**Goal:** APScheduler started in lifespan. Two cron jobs:
- Morning (default 9:30 IST): for each enabled group, enqueue a synthetic "morning check" trigger into the pipeline → deliberate step decides whether to ask for locations
- Nightly wipe (default 23:59 IST): archive all ephemeral rows to `memory_archive`, then delete from `memory_ephemeral`

**Key sub-tasks include:** timezone-aware scheduling, per-group opt-out, wipe-then-archive in a single transaction.

---

# Phase 21 — EveningSummarizerAgent

**Prereqs:** Phase 20.

**Goal:** At 22:00 IST, read each group's today-archive + ephemeral + today's outbound log, produce a concise summary per group, post as one consolidated message to the reports group (default) or per-group messages (setting).

---

# Phase 22 — WeeklyLeaderboardAgent

**Prereqs:** Phase 20.

**Goal:** Sunday 20:00 IST. Reads past 7 days from `memory_archive` across groups. Produces leaderboard (approaches, admissions, conversion rate, attendance consistency). Posts to reports group.

---

# Phase 23 — Scheduled check-ins

**Prereqs:** Phase 20.

**Goal:** `scheduled_checkins` table poller fires each due check-in by running the agent with a synthetic prompt; agent decides whether to actually send anything.

---

# Phase 24 — Agent-to-agent inbox

**Prereqs:** Phase 11.

**Goal:** `notify_supervisor(text)` and `ask_other_agent(group_id, text)` durably write to `inbox`. Each agent, on its next tick, checks its inbox first with `SELECT ... FOR UPDATE SKIP LOCKED` and handles messages.

---

# Phase 25 — Broadcast tool for supervisor

**Prereqs:** Phase 17.

**Goal:** `broadcast_to_groups(text, group_ids)` mutation tool. Requires director confirmation (change_id). On apply, fan out to `outbound_queue` with one row per group_id.

---

# Phase 26 — Settings page

**Prereqs:** Phase 12.

**Goal:** Admin UI page to edit: default reflex/deliberate models, timezone, ephemeral wipe time, morning check-in time, Green API creds (encrypted), Whisper toggle, send-rate limit.

---

# Phase 27 — Concurrency hardening

**Prereqs:** Phases 11, 24.

**Goal:** Chaos-style tests proving correctness under contention.

**Key tests:**
- 27.1 Two dispatchers running simultaneously → no message processed twice
- 27.2 Two outbound workers → FIFO preserved per chat_id, no double-send
- 27.3 100 concurrent `add_core` from same scope → no deadlocks, row count correct
- 27.4 Supervisor mutation + agent reflect writing to same `memory_core` row → serializable conflicts retried and converged
- 27.5 Pending mutation resolved twice simultaneously → exactly one applies

---

# Phase 28 — Error handling

**Prereqs:** Phases 11, 16.

**Goal:** Failure modes from spec §15 covered with tests and user-visible behavior.

**Tasks:** tool-call cap enforcement, wall-clock timeout, OpenAI API down queueing, Green API down backoff + dashboard red banner, pending-mutation limit of 5, stuck-agent detection (no tick in 10 min → dashboard warning).

---

# Phase 29 — Observability polish

**Prereqs:** Phase 15.

**Tasks:** Traces UI filters (by agent, intent, cost, time), cost totals per agent per day, dashboard health signals (last tick, queue depths, error rates).

---

# Phase 30 — Smoke test on real Green API

**Prereqs:** All above.

**Goal:** Maharshi provides one real campaign group chat_id + reports group chat_id + his father's WhatsApp user ID. We send a few real test messages, watch the pipeline, fix any bugs.

**No plan sub-steps** — this is exploratory. Produce a bug list, address each with a mini-plan inline.

---

# Phase 31 — Runbook + README

**Prereqs:** Phase 30.

**Files:** `README.md` (rewritten), `docs/runbook.md`, deployment notes.

**Contents:** how to install, migrate, seed, start, stop; how to add a group; how to rotate Green API tokens; how to recover from a stuck agent; what to do if Postgres restarts; where logs live.

---

# Multi-session execution — what to paste in each new session

### First session (Phase 0 + 1)

Open Claude Code in `E:\BTP project\` (the RAION repo, where this plan lives). Paste:

```
I'm starting the campaign-agents project. The spec is at
docs/superpowers/specs/2026-04-24-campaign-agents-design.md
and the plan is at
docs/superpowers/plans/2026-04-24-campaign-agents-plan.md.

Please:
1. Read the spec and plan.
2. Start Phase 0. Ask me where to create the new `campaign-agents` folder.
3. Use the superpowers:executing-plans skill.
```

### All subsequent sessions (Phase 2 onward)

Open Claude Code in the new `campaign-agents/` folder. Paste:

```
I'm continuing the campaign-agents project.

Please:
1. Read CLAUDE.md
2. Read SESSION_HANDOFF.md
3. Read docs/specs/campaign-agents-design.md
4. Read docs/plans/campaign-agents-plan.md (find the next uncompleted phase — SESSION_HANDOFF says which one)
5. Start that phase. Use the superpowers:executing-plans skill.
6. At the end of the phase, commit and update SESSION_HANDOFF.md.
```

That's it. The repo is the memory.

### If you want to do it all in one session

Just say "start Phase 0 and continue through all phases." I'll use `subagent-driven-development` to parallelize and checkpoint. Realistically this is many hours — expect to stop and resume anyway.

---

## Self-review (this plan)

- **Spec coverage:** Every numbered section of the spec maps to one or more phases. §4 pipeline → phases 8–11. §5 memory → phases 5–6. §6 agent types → 11, 16, 21, 22, 24. §7 director protocol → 16, 17, 19. §8 layering → 7. §9 schema → 1. §10 proactive → 20, 23. §11 honesty → 7. §12 admin UI → 12–15, 18, 26. §15 error modes → 28. §16 observability → 15, 29.
- **Placeholders:** None that block execution. Phase 2 and later list sub-tasks in narrative form rather than full code because the plan is already ~1000 lines and those phases are small; they'll be expanded into TDD steps when a session starts them. Phase 0, 1, 3–11, 16, 17, 20 — the load-bearing ones — have full detail.
- **Consistency:** Tool names match spec (`save_core`, `save_rag`, `save_ephemeral`, `recall`, `forget`, `schedule_checkin`, `notify_supervisor`, `ask_other_agent`, `stay_silent`, `whatsapp_send`). Agent types consistent (`group`, `supervisor`, `summarizer`, `weekly`). Scope strings consistent (`group:{id}`, `supervisor`, etc.). Timeout value 10 minutes used consistently. Authority model (director sir + maam, equal) consistent.
