# RAION — Project Context for Claude

## What this project is

RAION is a personal AI assistant web app (FastAPI + Jinja2 + Tailwind CSS) built by Maharshi Nahar. It is a self-hosted ChatGPT-like interface that runs locally, backed by OpenAI models, with a multi-agent supervisor/worker architecture, automation engine, MCP connector system, memory, skills, and Telegram integration.

---

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2 (async), SQLite (`app.db`) |
| Frontend | Jinja2 templates, Tailwind CSS (CDN `tailwind.min.js`), HTMX, vanilla JS |
| AI / Agents | LangGraph, LangChain OpenAI, OpenAI API (gpt-4o, gpt-4o-mini, etc.) |
| MCP | `mcp` SDK + `langchain-mcp-adapters` — stdio and SSE transports |
| Auth | Session cookies via `itsdangerous`, bcrypt password hashing |
| Scheduling | APScheduler (automation cron jobs) |
| Notifications | Telegram Bot API (send + bidirectional webhook) |
| Google | Gmail, Drive, Calendar via OAuth2 (Desktop app type), Fernet-encrypted tokens |

---

## Repo layout

```
app/
  config.py               — env vars (loaded from .env)
  main.py                 — FastAPI app factory, startup/shutdown hooks
  db/
    engine.py             — SQLAlchemy async engine + Base
    models.py             — ALL SQLAlchemy models
    seed.py               — seeds admin user on first run
  agents/
    supervisor.py         — LangGraph multi-agent graph (supervisor + workers)
    auto_memory.py        — auto-extract facts from conversations
  automations/
    parser.py             — NL → structured automation spec (OpenAI structured output)
    runtime.py            — automation trigger runtime (APScheduler)
    conversations.py      — multi-round Telegram conversation state machine
  mcp/
    manager.py            — MCPManager singleton: connect/disconnect/tool discovery
    loader.py             — TTL-cached tool loader used by the agent
    crypto.py             — Fernet encrypt/decrypt for MCP env vars
  permissions/
    policy.py             — tool permission policy (auto / ask / deny)
  tools/
    filesystem.py         — read/write/list/delete workspace files
    web.py                — web_search (DDGS), web_fetch
    google_tools.py       — Gmail, Drive, Calendar LangChain tools
    telegram_tools.py     — telegram_send, telegram_ask, save_draft, schedule_message
    skills.py             — read_skill tool (injects skill markdown into agent context)
    shell.py              — run_shell_command
  web/
    deps.py               — require_user FastAPI dependency
    routes/
      auth.py             — /login /logout
      chat.py             — /api/threads, /api/threads/{id}/messages, /api/upload
      ws.py               — WebSocket /ws/threads/{id} (streaming tokens)
      connectors.py       — /connectors, /api/connectors (MCP server CRUD)
      settings.py         — /settings (workspace dir, Telegram, models, Google, password)
      memory.py           — /memory, /api/memory
      automations.py      — /automations, /api/automations
      skills.py           — /skills, /api/skills
      tasks.py            — /tasks (scheduled tasks)
      audit.py            — /audit (permission audit log)
      telegram.py         — /webhook/telegram (incoming Telegram messages)
      permissions.py      — /api/permissions/{id} (approve/deny tool calls)
      health.py           — /health
    templates/
      base.html           — base layout, dark theme CSS overrides, theme toggle JS
      index.html          — main chat UI (sidebar, message pane, WebSocket client)
      settings.html       — settings page (workspace, Telegram, models, Google, password)
      connectors.html     — MCP connector management UI
      memory.html         — memory CRUD UI
      automations.html    — automation CRUD UI
      skills.html         — skills upload/management UI
      audit.html          — audit log UI
      login.html          — login page
    static/
      tailwind.min.js     — Tailwind CSS CDN (offline copy)
      htmx.min.js         — HTMX
workspace/                — default user workspace dir (files the agent reads/writes)
run.py                    — entry point: `python run.py`
requirements.txt
.env                      — secrets (never commit)
.env.example              — template for secrets
```

---

## Key env vars (`.env`)

```
SECRET_KEY=
DATABASE_URL=sqlite+aiosqlite:///./app.db
ADMIN_USERNAME=maharshi
ADMIN_PASSWORD=
OPENAI_API_KEY=
WORKSPACE_DIR=./workspace
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
FERNET_KEY=                  # encrypt Google tokens + MCP env vars at rest
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_WEBHOOK_URL=        # public HTTPS URL for Telegram webhook (ngrok etc.)
TELEGRAM_WEBHOOK_SECRET=
```

---

## MCP Connector system

### How it works

1. User adds a connector in `/connectors` — gives it a **Name** (e.g. `github`), a **Command** or **URL**, and any **env vars** (API tokens).
2. `MCPManager` spawns a subprocess (stdio) or opens an HTTP connection (SSE), runs the MCP protocol, and discovers tools.
3. Tool names are prefixed: `mcp__<name>__<tool_name>` — e.g. `mcp__github__create_issue`.
4. A skill file is auto-generated at `workspace/skills/mcp_<name>.md` and registered in the DB so the agent knows to use it.
5. The agent loads MCP tools dynamically via `load_active_mcp_tools()` (15s TTL cache).

### Tool name prefix rule

If you name the connector `github`, all its tools will be callable as:
```
mcp__github__<tool_name>
```

### Common MCP servers (how to add them)

| Service | Name | Transport | Command | Env var key | Env var value |
|---------|------|-----------|---------|-------------|---------------|
| Notion | `notion` | stdio | `npx -y @notionhq/notion-mcp-server` | `OPENAPI_MCP_HEADERS` | `{"Authorization": "Bearer secret_xxx"}` |
| GitHub | `github` | stdio | `npx -y @modelcontextprotocol/server-github` | `GITHUB_PERSONAL_ACCESS_TOKEN` | `ghp_xxx` |
| Brave Search | `brave` | stdio | `npx -y @modelcontextprotocol/server-brave-search` | `BRAVE_API_KEY` | `BSA...` |
| Filesystem | `fs` | stdio | `npx -y @modelcontextprotocol/server-filesystem /path` | _(none)_ | _(none)_ |
| Postgres | `postgres` | stdio | `npx -y @modelcontextprotocol/server-postgres` | `POSTGRES_CONNECTION_STRING` | `postgresql://...` |
| Slack | `slack` | stdio | `npx -y @modelcontextprotocol/server-slack` | `SLACK_BOT_TOKEN` | `xoxb-...` |
| Linear | `linear` | stdio | `npx -y @linear/mcp-server` | `LINEAR_API_KEY` | `lin_api_...` |

Find more MCP servers at: https://github.com/modelcontextprotocol/servers

---

## Agent architecture

- **Supervisor node**: main LangGraph node — calls tools or spawns worker sub-agents
- **Workers**: parallel sub-agents spawned via `spawn_worker` tool, each with their own tool execution loop
- **Bounds**: max 3 recursion depth, 10 agents, 50 tool calls, 10 min wall clock
- **Checkpointing**: LangGraph checkpoint via SQLite (or Postgres if configured)
- **Tools available to agent**: filesystem, web_search, web_fetch, google (Gmail/Drive/Calendar), telegram, shell, MCP tools, skills reader, spawn_worker

## Permissions

Every tool call goes through `policy.py`. Permission modes per tool:
- `auto` — runs without asking
- `ask` — pauses graph, sends a permission card to the chat UI, resumes after user approves/denies
- `deny` — always blocked

MCP tools default to `ask` when first discovered.

---

## Automation system

- Triggers: `cron`, `gmail_any_new`, `gmail_new_from_sender`, `gmail_keyword_match`, `fs_new_in_folder`
- User describes automation in natural language → `parser.py` (OpenAI structured output) converts to spec
- `runtime.py` runs triggers via APScheduler
- Telegram integration: `telegram_send` (one-way notify) vs `telegram_ask` (bidirectional, waits for user reply)
- Multi-round conversations tracked in `AutomationConversation` table with `conversation_id`

---

## Dark theme

Dark theme is toggled via `data-theme="dark"` on `<html id="html-root">`. Stored in `localStorage`. Theme is applied before paint (inline script in `<head>`) to avoid FOUC.

All dark mode CSS is in `base.html` `<style>` block using `[data-theme="dark"] .class` selectors with `!important`. The sidebar (`bg-gray-900`) is always dark regardless of theme — this is intentional.

---

## Development

```bash
python run.py          # starts uvicorn on port 8000
```

No build step — Tailwind is the CDN/offline copy. No frontend bundler.

---

## Phases completed

- Phase 0: Auth (login/session)
- Phase 1: Chat threads + OpenAI streaming
- Phase 2: WebSocket streaming
- Phase 3: Multi-agent supervisor/worker graph
- Phase 4: Permissions (interrupt/resume)
- Phase 5: Workspace filesystem tools
- Phase 6: Web search + fetch
- Phase 7: Google OAuth (Gmail, Drive, Calendar)
- Phase 8: Telegram send + bidirectional webhook
- Phase 9: Automations (NL parser + APScheduler runtime)
- Phase 10: MCP connector system
- Phase 11: Memory (manual + auto-extract)
- Phase 12: Skills (upload markdown skill files, / autocomplete in chat)
- Phase 13: Scheduled tasks

---

## Coding conventions

- Python: async everywhere (`async def`, `await`), type hints, `from __future__ import annotations`
- No global state except singletons (`MCPManager`, LangGraph graph)
- SQLAlchemy: always use `AsyncSession`, never sync
- Templates: Jinja2 extends `base.html`, Tailwind utility classes only (no custom CSS files)
- JS: vanilla only, no npm, no bundler
- Dark theme: add overrides in `base.html` `<style>` block, not inline styles
