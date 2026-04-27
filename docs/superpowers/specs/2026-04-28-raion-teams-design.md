# RAION Teams — Design Spec
**Date:** 2026-04-28  
**Status:** Approved for implementation  
**Author:** Maharshi Nahar + Claude

---

## Overview

RAION Teams is a multi-agent OS built inside RAION. It extends RAION's existing supervisor/worker architecture with persistent, named agents that run 24/7, have layered memory, manage their own cron jobs, communicate with each other, and can be deployed locally or on LAN nodes. RAION remains the single control plane. Remote PCs are dumb execution nodes.

---

## Architecture — Approach B (Agent Runtime inside RAION)

Each agent is a first-class DB entity with:
- Its own event loop slot managed by APScheduler
- A task queue (priority-ordered)
- Four-tier memory system
- A seat registry (input/output surfaces)
- A self-managed cron registry
- A position in the agent hierarchy
- An environment boundary

RAION's existing systems (WebSocket hub, Green API, Telegram, APScheduler, LangGraph) are reused. No new process model for local agents. LAN nodes run a thin Node Runner script that connects back to RAION via WebSocket.

---

## Data Model

### `agent_environments`
```
id, name, slug, description, color,
default_node_id (FK agent_nodes, nullable),
created_at
```

### `agents`
```
id, name, slug, persona (system prompt base),
status (active / paused / archived / error),
environment_id (FK agent_environments, nullable — null = system agent),
parent_agent_id (FK self, nullable),
host_node_id (FK agent_nodes, nullable — null = local),
wake_mode (always_on / event_only / scheduled_only / adaptive / manual),
tick_interval_seconds (default 30),
is_system_agent (bool, default false, max one row true),
current_version (int),
created_at, updated_at, created_by (user / agent_id)
```

### `agent_versions`
```
id, agent_id (FK), version_number,
config_snapshot_json (full agent config + important memory + seats + tools),
created_at, created_by (user / agent_id)
```
Every save to agent config auto-inserts a new version row. Rollback = deserialize snapshot JSON → overwrite current config.

### `agent_seats`
```
id, agent_id (FK),
seat_type (whatsapp_group / folder / telegram_chat / webhook / cron_poll),
config_json, output_channels_json,
enabled, last_event_at, created_at
```

### `agent_inbox`
```
id, agent_id (FK), source_type (seat_type / agent_message / webhook),
source_id, payload_json, priority (urgent / normal / background),
status (pending / processing / done / dropped),
created_at, processed_at
```
All wakeup events (webhook, watchdog, agent message, cron fire) insert a row here. Agent drains queue sequentially.

### `agent_memory`
```
id, agent_id (FK),
tier (ephemeral / important / skill / rag),
key, content,
embedding (vector, for RAG tier only),
token_count, access_count, last_accessed_at,
promoted_from_tier (nullable),
ttl_expires_at (nullable — ephemeral only),
created_at
```

### `agent_crons`
```
id, agent_id (FK), name, cron_expr,
action_prompt, enabled,
state_json (arbitrary agent-managed state),
last_run_at, next_run_at, created_at
```

### `agent_messages`
```
id, from_agent_id (FK, nullable — null = user),
to_agent_id (FK),
message_type (task / report / query / alert / council_invite),
content, thread_id (grouping),
status (pending / read / replied),
created_at, read_at
```

### `agent_blackboard`
```
id, council_id, agent_id (FK, nullable — null = user),
entry_type (analysis / vote / question / answer / decision),
content, created_at
```

### `councils`
```
id, topic, chair_agent_id (FK),
environment_id (FK, nullable),
status (active / concluded),
deadline_at, created_at, concluded_at
```

### `agent_nodes`
```
id, name, host, ws_url,
status (online / offline),
capabilities_json (["filesystem","shell","playwright","python","gpu"]),
last_ping_at, created_at
```

### `agent_metrics`
```
id, agent_id (FK), date (date),
tick_count, event_count, llm_calls,
tokens_used, estimated_cost_usd,
actions_taken, errors, created_at
```

### `agent_templates`
```
id, name, description, category,
config_json (persona, default seats, default tools, default important memory),
created_by (user / agent_id), is_builtin (bool),
created_at
```

---

## Wakeup Model — Event-Driven + Tick Hybrid

Every agent has two wakeup mechanisms running in parallel:

### 1. Instant Wakeup (Push)
Fires immediately, inserts high-priority row into `agent_inbox`:
- Another agent sends a message (`agent_messages` insert trigger)
- Webhook arrives at `/webhook/agent/<slug>`
- `watchdog` OS-level file event fires on a folder seat
- Telegram message arrives on a telegram seat
- WhatsApp webhook fires for a watched group
- Council invite received

### 2. Scheduled Tick (Pull)
For absence detection, polling seats without webhooks, memory promotion evaluation, cron evaluation. Runs at `tick_interval_seconds`. Can be disabled for event-only agents.

### Wake Modes

| Mode | Behavior | Best for |
|---|---|---|
| `always_on` | Instant wakeup + scheduled ticks | WhatsApp watchers, active monitors |
| `event_only` | Instant wakeup only, no ticks | Reactive specialists |
| `scheduled_only` | Only ticks, no instant wakeup | Background report agents |
| `adaptive` | Tick interval shrinks when busy, grows when quiet | General purpose |
| `manual` | Only wakes when triggered via UI or system agent | Dormant / on-demand |

**Adaptive mode intervals:**
- Active (events firing): 10s tick
- Normal: 30s tick
- Quiet (no events for 10min): 5min tick
- Deep quiet (no events for 1hr): 15min tick

---

## Agent Tick Pipeline

```
AgentTick(agent_id)
  │
  ├── 1. DRAIN INBOX
  │     Pop next item(s) from agent_inbox by priority (urgent first)
  │     If inbox empty AND mode=scheduled_only → run scheduled fetch
  │     If inbox empty AND no scheduled fetch due → EXIT (no-op, no LLM call)
  │
  ├── 2. BUILD CONTEXT
  │     Load important memory → inject into system prompt
  │     Load skill memory index (names + descriptions) → inject into system prompt
  │     Load ephemeral memory → inject as recent notes
  │     RAG search against current inbox content → inject top-K chunks
  │
  ├── 3. DECIDE & ACT (LangGraph invoke)
  │     LLM sees: system prompt + memory blocks + inbox events
  │     Available tools: all standard RAION tools +
  │       message_agent, invoke_agent, convene_council, post_to_blackboard,
  │       create_agent_cron, edit_agent_cron, delete_agent_cron,
  │       promote_memory, write_memory, delete_memory,
  │       create_agent (spawns new agent, starts paused, notifies user),
  │       spawn_workers (same as supervisor — parallel task workers)
  │
  ├── 4. STORE OUTPUTS
  │     New events → RAG memory (chunked + embedded)
  │     Agent decisions/notes → ephemeral memory (with TTL)
  │     Update seat cursors (last_event_at per seat)
  │     Update agent_metrics row for today
  │
  └── 5. MEMORY PROMOTION CHECK (async, non-blocking)
        Runs in background after tick completes
        RAG entries accessed 5+ times in 7 days → promote to skill memory
        RAG entries accessed 10+ times in 7 days AND factual → propose important memory promotion (notify user)
        Important memory entries not accessed in 30 days → demote to RAG
        Ephemeral entries past TTL → delete
        Enforce important memory token budget (2000 tokens default, configurable)
```

**Single-agent sequential processing:** One tick at a time per agent. Next tick waits until current completes. No race conditions on memory writes.

**Cost-saving no-op:** If inbox is empty and no scheduled fetch is due, the LLM is not called. Cheap DB read exits early.

**Tick timeout:** 120 seconds per tick. If exceeded, tick is killed and logged as error. Next tick fires normally.

**Agent-to-agent round-trip latency:** ~2-5 seconds (both agents on instant wakeup mode).

---

## Memory System

### Four Tiers

**Ephemeral**
- Short-term scratchpad. Auto-deleted on TTL expiry (default 24h, configurable per entry).
- Injected as "recent notes" block in context.
- Agent can explicitly delete entries mid-task.
- Not promoted — intentionally temporary.

**Important**
- Core facts always present in system prompt.
- Hard token budget: 2000 tokens (configurable per agent).
- Initial set written by user at agent creation.
- Agent can propose additions → user notified in UI + Telegram → approve/veto.
- Agent can force-promote mid-tick if immediately critical (still notifies user).
- Demoted automatically if not accessed in 30 days (frees budget).

**Skill**
- Procedural knowledge. Only name + trigger description in system prompt.
- Full content loaded on demand (same pattern as RAION's `read_skill()`).
- Agent can write new skill entries after learning a repeatable pattern.
- No token budget pressure.

**RAG**
- Everything the agent has ever seen. Chunked + embedded.
- Semantic search at context-build time → top-K injected.
- Max 10,000 chunks per agent. Pruning: oldest + least-accessed first when full.
- Agent can explicitly forget a topic (deletes matching chunks).

### Promotion Pipeline
Runs hourly as background task:
```
RAG (access_count >= 5 in 7 days) → Skill Memory
RAG (access_count >= 10 in 7 days, factual) → Important Memory (pending user approval)
Important Memory (not accessed in 30 days) → RAG (demote, free budget)
Ephemeral (TTL expired) → Delete
```
Thresholds are configurable per agent. Agent can also call `promote_memory(key, target_tier)` as a tool to force-promote during any tick.

### Manual Promotion via UI
Memory tab on agent detail page shows all four tiers. Per-entry actions: Promote (pick tier), Demote, Edit, Delete. Important memory tab shows token budget usage bar.

---

## Inter-Agent Communication

### Mode 1 — Async Message Passing
- `message_agent(to, type, content)` → inserts row in `agent_messages` → triggers instant wakeup on recipient
- Message types: `task`, `report`, `query`, `alert`, `council_invite`
- `alert` type = high priority, jumps queue
- Round-trip: ~2-5 seconds on instant wakeup agents

### Mode 2 — Direct Invocation
- `invoke_agent(to, query)` → triggers mini-tick on target agent with just the query
- Blocks until response (timeout: 2 minutes)
- Max chain depth: 3 (A→B→C, C cannot invoke D)
- Only works for online agents (local or online LAN node)

### Mode 3 — Council
- `convene_council(topic, agents[], deadline)` → creates council row + blackboard + sends `council_invite` to all participants
- Each participant reads blackboard on wakeup, writes their contribution (analysis / vote / question / answer)
- Chair agent reads all contributions → synthesizes → writes `decision` entry → sends summary to user via Telegram
- User can also convene a council from the UI (`/teams/councils → + Convene Council`)
- Cross-environment councils: only System Agent can invite agents from different environments

### Environment Isolation Rule
Agents in Environment A cannot message, invoke, or council with agents in Environment B.
**Exception:** System Agent (`is_system_agent=true`) has no environment, can reach any agent in any environment.

---

## Environment System

### What environments provide
- Isolation boundary — agents inside cannot communicate with agents outside
- Shared blackboard accessible to all agents within the environment
- Default node, default tools, default tick interval inherited by new agents
- Independent color coding in UI for visual separation

### System Agent
- One special agent with `is_system_agent=true`, `environment_id=null`
- Only user can create it
- Sits above all environments, can reach any agent anywhere
- Can convene cross-environment councils
- Can create agents in any environment
- Appears in UI above all environment cards with a special "System" badge
- Use case: meta-supervisor, consolidated reporting, cross-team coordination

---

## Sitting System — Seat Types

### WhatsApp Group Seat
- Input: RAION webhook (instant) + poll fallback every 30s
- Sees: messages, media, sender info
- Acts via: `whatsapp_send`, `whatsapp_send_file`
- Config: `{ group_id, keyword_filter (optional), vision_enabled }`

### Folder Seat
- Input: Python `watchdog` library (OS-level, near-instant)
- Sees: file created, modified, deleted, moved — with content
- Acts via: `write_file`, `delete_file`, `move_file`, `run_shell_command`
- Config: `{ folder_path, watch_recursive, file_extensions_filter, ignore_patterns }`
- Note: watchdog runs on whatever node the agent is assigned to

### Telegram Chat Seat
- Input: RAION webhook (instant)
- Sees: messages in specific chat/group
- Acts via: `telegram_send`, `telegram_ask`, `telegram_send_file`
- Config: `{ chat_id, respond_to_all }`

### Webhook Seat
- Input: dedicated endpoint `/webhook/agent/<slug>` in RAION
- Sees: any POST payload
- Acts via: any allowed tool
- Config: `{ secret_token, payload_schema (optional) }`
- Use case: GitHub events, Zapier, any external service

### Cron Poll Seat
- Input: scheduled only
- Sees: result of fetch (Gmail / URL / shell command / DB query)
- Acts via: any allowed tool
- Config: `{ cron_expr, fetch_target }`

### Agent Inbox Seat *(always present, not configurable)*
- Every agent always has this
- Input: instant wakeup when `agent_messages` row arrives for this agent
- Cannot be disabled

---

## LAN Node System

### Node Runner (remote PC)
Thin Python script (~150 lines):
```bash
python node_runner.py --raion-url ws://192.168.1.x:8000 --node-name "office-pc" --token <secret>
```
- Connects to RAION via WebSocket
- Declares capabilities: `["filesystem", "shell", "playwright", "python", "gpu"]`
- Receives tick payloads from RAION for assigned agents
- Executes ticks locally (accesses local files, local shell, local browser)
- Streams results back to RAION
- Sends ping every 10s

### Health & Recovery
- RAION marks node offline after 3 missed pings (30s)
- Agent ticks pause when node is offline
- User gets Telegram notification on offline/online transitions
- `agent_inbox` events queue in DB while node is offline — processed on reconnect
- Capabilities declared by node shown in agent creation UI ("this agent needs Playwright — assign to a node with playwright capability")

### What lives where
| Thing | Lives on |
|---|---|
| Agent DB, memory, config, versions | RAION (always central) |
| Tick execution | Assigned node |
| File/shell tool calls | Node (that PC's filesystem) |
| WhatsApp/Telegram sends | RAION (credentials are central) |
| WebSocket connection | Node Runner maintains |

---

## Agent Versioning

Every save to agent config (persona, important memory, seats, tools, tick interval, wake mode) auto-inserts a row in `agent_versions` with a full JSON snapshot of the config at that point.

**UI:** Version history tab on agent detail page shows all versions with timestamp and what changed (diff view). One-click rollback to any version. Rollback itself creates a new version (so rollback is also undoable).

**Agent self-modification:** When an agent modifies its own config (e.g. promotes memory, edits its persona via tool), this also creates a version with `created_by = agent_id`. User can see exactly what the agent changed and roll it back if needed.

---

## Agent Templates

Pre-built or user-saved configurations for common agent types.

**Built-in templates:**
- `whatsapp-group-monitor` — watches a WhatsApp group, summarizes daily, alerts on keywords
- `folder-processor` — watches a folder, processes new files, notifies on Telegram
- `email-monitor` — polls Gmail, classifies emails, drafts replies
- `research-agent` — web search + RAG, answers queries from other agents
- `campaign-manager` — tracks staff submissions, sends reminders, escalates

**User-saved templates:** Any agent config can be saved as a template. Templates show in the "Create Agent" wizard as a starting point. Cloning a template creates a new independent agent — changes don't affect the template.

**Agent-created templates:** An agent can call `save_as_template(name, description)` to save its own current config as a template for others to use.

---

## Metrics & Analytics

`agent_metrics` table tracks per-agent, per-day:
- `tick_count` — how many ticks ran
- `event_count` — how many inbox events processed
- `llm_calls` — how many LLM invocations (ticks that weren't no-ops)
- `tokens_used` — total tokens across all LLM calls that day
- `estimated_cost_usd` — tokens × model pricing
- `actions_taken` — tool calls executed
- `errors` — failed ticks

**UI — Metrics tab on agent detail page:**
- 7-day / 30-day charts for all above metrics
- Cost breakdown (per day, cumulative)
- Most-used tools
- Error log with tick replay (see what the agent was doing when it errored)

**UI — Teams overview page:**
- Total cost across all agents today / this month
- Most active agent, most expensive agent
- System-wide error rate

---

## UI Structure

### Sidebar
```
RAION sidebar
  ├── Chat
  ├── Automations
  ├── Skills
  ├── Memory
  ├── Teams
  │     ├── Agents
  │     ├── Environments
  │     ├── Nodes
  │     ├── Councils
  │     ├── Templates
  │     └── Analytics
  └── Settings
```

### Routes
```
/teams                          → overview (all environments, agent count, cost today)
/teams/agents                   → all agents across all environments
/teams/agents/<slug>            → agent detail (6 tabs: Overview, Memory, Crons, Hierarchy, Metrics, Versions)
/teams/agents/<slug>/edit       → edit agent config (wizard)
/teams/environments             → environment list
/teams/environments/<slug>      → environment detail (agents inside, shared blackboard)
/teams/nodes                    → LAN node list + registration
/teams/councils                 → active + past councils
/teams/templates                → template library
/teams/analytics                → system-wide metrics
/webhook/agent/<slug>           → webhook seat endpoint (POST)
```

### Agent Creation Wizard (5 steps)
1. **Identity** — name, slug, persona, parent agent, node, wake mode, tick interval
2. **Seats** — add/configure seats (WhatsApp, folder, Telegram, webhook, cron poll)
3. **Memory Foundation** — write initial important memory entries, token budget shown
4. **Tools & Permissions** — checkbox list of allowed tools, permission mode per tool
5. **Review & Launch** — summary card, [Launch Agent] button

Agent-created agents start **paused** by default. User gets Telegram notification with approve/veto link.

---

## Use Cases

### Easy
**WhatsApp group summarizer:**
Agent sits in a WhatsApp group, sends a daily 9pm summary to Maharshi on Telegram. Self-creates a cron at 9pm. Stores all messages in RAG. No hierarchy needed.

**Folder invoice processor:**
Agent watches `\\office-pc\invoices\incoming\`. New PDF arrives → instant wakeup → reads PDF → extracts amount + vendor → writes to a log file → sends Telegram notification. Runs on office-pc node.

### Medium
**Campaign submission tracker:**
Ops Manager agent sits in staff WhatsApp group. Knows deadline is Friday 3pm. Self-creates crons to remind non-submitters. Tracks who submitted in `state_json`. If all submit early, cancels reminder cron. Escalates to Maharshi via Telegram if anyone hasn't submitted by 2:30pm.

**Cross-agent research:**
Research Lead receives task via Telegram. Invokes Web Scraper agent (direct invocation) for current data. Async messages Data Analyst agent to process results. When both report back, synthesizes final report, writes to Drive, sends Telegram summary.

### Hard
**Distributed campaign management:**
System Agent oversees Business environment. Ops Manager monitors staff WhatsApp. Finance Agent watches invoice folder on office-pc. Research Agent polls competitor pricing daily.

Every Friday 2pm: Ops Manager convenes council with Finance Agent + Research Agent. Each contributes — ops status, financial summary, market context. Chair (Ops Manager) synthesizes into weekly report. System Agent receives report, cross-references with Personal environment calendar agent, sends consolidated briefing to Maharshi.

If any staff member goes silent for 3+ hours during campaign day: Ops Manager's watchdog cron fires, escalates to System Agent via alert message, System Agent sends urgent Telegram to Maharshi.

---

## New Tools Added to Agent Tool Registry

| Tool | Description |
|---|---|
| `message_agent(to, type, content)` | Async message to another agent |
| `invoke_agent(to, query)` | Synchronous query to another agent (2min timeout) |
| `convene_council(topic, agents, deadline)` | Start a council session |
| `post_to_blackboard(council_id, entry_type, content)` | Write to council blackboard |
| `create_agent_cron(name, cron_expr, action_prompt)` | Self-create a cron job |
| `edit_agent_cron(id, ...)` | Modify own cron job |
| `delete_agent_cron(id)` | Remove own cron job |
| `promote_memory(key, target_tier)` | Force-promote a memory entry |
| `write_memory(tier, key, content, ttl?)` | Write to own memory |
| `delete_memory(key)` | Delete a memory entry |
| `create_agent(name, persona, seats, ...)` | Spawn a new agent (starts paused) |
| `save_as_template(name, description)` | Save own config as template |

---

## Phases — Suggested Build Order

1. **DB models + migrations** — all new tables
2. **Agent Runtime** — tick pipeline, inbox queue, wakeup mechanisms, APScheduler slots
3. **Memory system** — four tiers, promotion pipeline, RAG embedding
4. **Seat integrations** — WhatsApp, folder (watchdog), Telegram, webhook, cron poll
5. **Inter-agent communication** — async messages, direct invocation, council + blackboard
6. **LAN Node Runner** — WebSocket protocol, node registration, remote tick execution
7. **Environment system + System Agent** — isolation rules, cross-environment council
8. **Versioning + Templates + Metrics** — agent versions, template library, analytics
9. **UI** — Teams section, all routes, agent wizard, memory UI, metrics charts
10. **Self-improvement loop** — agent creates crons, promotes memory, spawns agents, saves templates
