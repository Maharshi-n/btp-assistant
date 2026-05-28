# Agents v2 — Design

Date: 2026-05-28
Author: Maharshi Nahar (with Claude)
Status: Approved (verbal), pending written review
Supersedes/extends: `docs/agent_system_plan.md`, the shipped `feat/agent-system` work

---

## 1. Goal

Make RAION's **Agent** feature production-grade and genuinely autonomous. An agent
should be a long-lived specialist that can:

1. Run stateful automations (wake on trigger → act → remember → sleep). *(exists)*
2. **Wake itself up at any time and create/modify/delete its own triggers.** *(new — headline)*
3. Have durable, non-clobbered memory plus searchable run history. *(upgrade)*
4. Be specialized in its role but still have RAION's full power. *(exists)*

Plus a fully rebuilt, polished `/agents` UI.

## 2. Non-goals

- Per-agent tool scoping. **Explicitly rejected by user** — agents always get the
  full RAION tool set; the "stay in your role" prompt block + autonomous-run
  directive are the guardrails.
- Vector/semantic recall. Keyword+date search over a per-agent JSONL log is enough
  at personal scale. Hook left open to add RAG later.
- Inter-agent messaging.
- A separate app/process. Agents live inside RAION.

## 3. Isolation principle (hard constraint from user)

> "It should be in RAION only. Separate means: if you use things from RAION code,
> don't change that shared code. If you want to change something in shared code,
> copy that code into a new file and change it there."

Rules I follow:
- All new logic lives under `app/agents/` (and agent-owned routes/templates).
- I do **not** edit `_supervisor_system_prompt`, the automation engine core
  (`_fire_automation`, `_gmail_poll`, `_NewFileHandler`), or any other shared
  RAION behavior.
- Where agent behavior must differ from a shared function, the function is **already
  forked** (`_agent_base_system_prompt`, `app/agents/agent_triggers.py`) — I extend
  the fork, never the original.
- "Shared code" means RAION's *behavior* (its prompt, its tool list, the automation
  engine). Agent-owned tables (`agents`, `agent_triggers`, `agent_runs`) and their
  migrations in `models.py` / `init_db()` are NOT RAION shared behavior — editing them
  is in-scope and does not violate the isolation rule.
- **One unavoidable shared-file touch:** `supervisor_node` (in `app/agents/supervisor.py`)
  binds the tool list. To give agents the new self-scheduling/recall tools I add a
  **purely additive branch**: `if agent_id_cfg: tools = SUPERVISOR_TOOLS + AGENT_ONLY_TOOLS`.
  RAION's path (`agent_id_cfg is None`) is byte-for-byte unchanged, so RAION cannot
  regress. The new tools themselves are defined in a new agent-owned module
  (`app/agents/agent_tools.py`). This is the minimal, regression-safe way to inject
  agent-only tools; documented here so the tradeoff is explicit.

## 4. Architecture overview

Unchanged core: agent = role + memory + triggers; wake-on-trigger → fresh thread →
run on the shared LangGraph supervisor (via `agent_id` in config, which selects
`_agent_base_system_prompt`) → distill memory → sleep. No always-on per-agent process.

New/changed components:

| Component | File | Status |
|-----------|------|--------|
| Self-scheduling + recall tools | `app/agents/agent_tools.py` (new) | new |
| `self_scheduled` one-shot trigger | `app/automations/runtime.py` `register_agent_trigger` | extend (agent branch only) |
| Self-wake chain depth / dedup / budget | `app/agents/agent_runtime.py` | extend |
| Episodic log (write + search) | `app/agents/agent_episodes.py` (new) | new |
| Protected manual-notes memory block | `app/agents/agent_memory.py` | extend |
| Agent tool binding branch | `app/agents/supervisor.py` `supervisor_node` | additive branch |
| Self-scheduling prompt section | `app/agents/supervisor.py` `_agent_base_system_prompt` | extend (agent prompt only) |
| Rebuilt dashboard + detail UI | `templates/agents.html`, `templates/agent_detail.html` | rebuild |
| Trigger CRUD + episodes endpoints | `app/web/routes/agents.py` | extend |

## 5. Self-scheduling (headline feature)

### 5.1 New trigger type `self_scheduled`
- Stored in existing `AgentTrigger` table: `trigger_type="self_scheduled"`,
  `trigger_config_json = {"fire_at": "<ISO8601>", "note": "...", "chain_depth": N}`.
- Registered via APScheduler `DateTrigger` (one-shot) in the **agent branch** of
  `register_agent_trigger`. After firing, the trigger row is deleted (one-off).
- `fire_agent` receives `chain_depth` so the run knows how deep the self-wake chain is.

### 5.2 Agent-only tools (in `app/agents/agent_tools.py`)
All resolve the current `agent_id` from run config and may only touch that agent's
own rows (enforced server-side).

- `agent_schedule_self(when: str, note: str)` — parse `when` ("in 2 hours",
  "tomorrow 9am", "18:00", ISO) → absolute timestamp → create a `self_scheduled`
  trigger. Primitive for "continue later / retry after delay".
- `agent_create_trigger(trigger_type, config, description)` — create a permanent
  recurring trigger (cron/gmail_*/fs_new_in_folder/whatsapp_*) on itself. Validated
  with the existing `validate_triggers`.
- `agent_list_triggers()` — list this agent's triggers (so it can avoid duplicates).
- `agent_delete_trigger(trigger_id)` — remove one of its own triggers (ownership checked).

### 5.3 Prompt
`_agent_base_system_prompt` gains a section (agent prompt only, RAION untouched):

> ━━━ YOU CONTROL YOUR OWN SCHEDULE ━━━
> You can wake yourself later (`agent_schedule_self`) and create or remove your own
> triggers (`agent_create_trigger` / `agent_delete_trigger` / `agent_list_triggers`).
> Use this when a task needs a follow-up, a retry after a delay, or new ongoing
> watching. Check `agent_list_triggers` before adding one so you don't duplicate.

Right-altitude: states the capability and when to use it, no rigid if/then.

### 5.4 Loop safety (budget + depth + dedup)
- **chain_depth**: a run fired from a `self_scheduled` trigger carries its
  `chain_depth`. Each `agent_schedule_self` call writes `chain_depth + 1`. At/above
  `SELF_WAKE_MAX_DEPTH` (default 5) the tool refuses and returns "self-wake chain
  limit reached — go idle." A run fired from a **user-created or external** trigger
  starts at `chain_depth = 0` (real-world event resets the chain).
- **dedup**: `agent_schedule_self` rejects a new wake-up whose `note` matches a
  pending `self_scheduled` trigger within `SELF_WAKE_DEDUP_WINDOW` (default 60s).
- **budget**: existing `daily_fire_budget` caps total wakes/day; self-wakes count.
- **pause**: a paused agent's `fire_agent` already returns `skipped:paused`; pending
  self-wakes are skipped (and cleaned up). Pause always wins.

## 6. Memory v2

### 6.1 Protected manual-notes block
Memory file template gains markers:
```
## Durable facts
...
## Open tasks
...
## Recent decisions
...
## People/contacts
...
<!-- MANUAL_NOTES_BEGIN -->
<!-- MANUAL_NOTES_END -->
```
`distil_memory`:
1. Read the current file, split out the `MANUAL_NOTES` block verbatim.
2. Send only the auto sections + new events to the distiller (prompt told never to
   emit the markers/manual content).
3. Re-assemble: distilled auto sections + the preserved manual block, atomic write.

This kills the "manual edit silently clobbered by re-distillation" bug. Same pattern
the DB-skill scanner already uses (`MANUAL_NOTES_BEGIN/END`), proven in this codebase.

### 6.2 Episodic log (`app/agents/agent_episodes.py`)
- File: `agents/<id>_episodes.jsonl`, append-only, one JSON object per run:
  `{"run_id", "ts", "trigger_type", "summary", "actions": [...]}`.
- Written at the end of `fire_agent` (reuses the `record` + `actions` already computed).
- Never rewritten, never distilled — durable audit trail.
- `agent_recall(query: str, since: str | None)` tool: keyword/substring match over
  `summary` + `actions`, optional date-floor filter; returns matching past runs.
  Answers "what did I see last week?" / "did I already notify about this?".

## 7. UI rebuild

All Tailwind utility classes + RAION dark theme (`base.html` CSS vars/overrides).
No `prompt()`/`alert()`/`confirm()` — real in-page panels and modals.

### 7.1 `/agents` dashboard (`agents.html`)
- Agent cards: name, role one-liner, status pill, **last run** (relative + outcome),
  **next wake** (soonest upcoming trigger incl. self-scheduled), fires-today/budget.
- Card actions: Pause/Resume, Open, Delete (in-page confirm modal).
- "New Agent" → in-page creation wizard: role → interview questions → review proposed
  role + triggers → create. Same `interview_questions`/`finalize_agent_spec` backend.

### 7.2 `/agents/{id}` detail (`agent_detail.html`)
- Header: name, status, model, fires-today/budget, next-wake.
- **Triggers panel**: each trigger's type + human-readable config + enabled toggle +
  delete; **self-created triggers badged** distinctly; "Add trigger" form.
- **Memory panel**: in-page editor (textarea + Save); manual-notes block visually
  demarcated so the user knows what survives distillation.
- **Activity timeline**: episodic log as a timeline — trigger, time, summary, actions,
  link to the run's thread (`/?thread=N`); searchable (same data as `agent_recall`).
- **Chat** button: existing `POST /api/agents/{id}/chat`.

### 7.3 Endpoints (extend `app/web/routes/agents.py`)
- `GET /api/agents/{id}/triggers`, `POST /api/agents/{id}/triggers`,
  `DELETE /api/agents/{id}/triggers/{tid}`.
- `GET /api/agents/{id}/episodes?q=&since=`.
- Existing memory GET/PUT kept; UI uses a panel instead of `prompt()`.

## 8. Data model changes

No new tables. `self_scheduled` reuses `AgentTrigger`. Episodic log is a file, not a
table. New module-level constants only.

`AgentTrigger.trigger_config_json` for `self_scheduled` carries `fire_at`, `note`,
`chain_depth`. A `created_by` distinction (self vs user) is needed for the UI badge:
store `"created_by": "self"|"user"` inside `trigger_config_json` (no schema change),
or add a nullable `created_by` column guarded by an idempotent `ALTER TABLE` in
`init_db()` (the codebase pattern). **Decision: add the nullable column** — cleaner
for querying/badging, and the project already does idempotent ALTERs.

> SCHEMA GOTCHA (known): production DB is PostgreSQL; `create_all` does NOT alter
> existing tables. Any new column MUST have an idempotent `ALTER TABLE ... ADD COLUMN`
> guard in `init_db()`.

## 9. Error handling

- Self-scheduling tools: invalid `when` → return a clear error to the agent (it can
  retry); never crash the run. Ownership violations → refuse.
- `register_agent_trigger` `self_scheduled` with a past `fire_at` → fire ~immediately
  (APScheduler misfire grace) or skip if too old; log it.
- Episodic log append is best-effort (wrapped) — a logging failure never fails a run.
- Memory split: if markers are missing/corrupt, treat the whole file as auto content
  (degrade gracefully, don't lose data) and re-inject empty markers.

## 10. Testing

- Unit: `when`-parsing; chain-depth increment + cap; dedup window; manual-notes
  preservation across a distil; episodic append + recall query/since filter;
  trigger ownership enforcement.
- Integration: self-scheduled trigger registers a one-shot job, fires, deletes itself;
  paused agent skips pending self-wake; `agent_create_trigger` round-trips through
  `validate_triggers`.
- Regression: RAION normal chat path (`agent_id_cfg is None`) binds the unchanged
  `SUPERVISOR_TOOLS` and uses `_supervisor_system_prompt` (assert no agent tools leak).

## 11. Open risks

- Shared `supervisor_node` touch (§3) — mitigated by the additive-branch design and
  the regression test in §10.
- `when` natural-language parsing — keep it simple (dateparser-style or a tiny LLM
  call); must always yield an absolute UTC timestamp or a clean error.
