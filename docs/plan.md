# Personal AI Assistant — BTP Project Plan

**Owner:** Maharshi
**Project dir:** `E:\BTP project\`
**Plan created:** 2026-04-10
**Intended executor:** Claude Sonnet (development work)

---

## How to use this plan

This plan is written so you can hand it to Claude Sonnet in a fresh session and have it execute one phase at a time, with you reviewing between phases. Read this section before starting.

### Session protocol for executing this plan

1. **Start a new Sonnet session in `E:\BTP project\`.**
2. **First message to Sonnet:** `Read C:\Users\mahar\.claude\plans\fizzy-beaming-hollerith.md in full, then execute Phase <N>. Do not start Phase <N+1> without my approval.`
3. **One phase per session is the default.** Each phase below is sized to fit comfortably in one working session with review time. Do not let Sonnet chain phases without you checking in.
4. **After every phase**, Sonnet must run the phase's **Verification** checklist and paste the results to you before claiming completion.
5. **Between phases, you commit to git.** Each phase ends in a committable state. Never start a new phase on top of uncommitted code from a prior phase.
6. **If a phase goes sideways**, tell Sonnet to stop, revert with `git reset --hard HEAD`, and restart the phase. Don't let half-finished phases pile up.

### Rules for Sonnet while executing any phase

- **Scope discipline:** Do not add features not listed in the current phase. Future phases exist; don't front-run them.
- **No new dependencies** beyond what this plan specifies without asking Maharshi first.
- **SQLite only** for persistence. Do not introduce Redis, RabbitMQ, Postgres, or any other DB.
- **Single process only.** No Docker, no multi-service deployment, no workers-as-separate-processes.
- **LangGraph is the agent runtime.** Do not invent parallel orchestration logic outside LangGraph.
- **Verification before completion:** Run the verification commands in the phase and paste output. No "should work" claims.
- **Commit at phase end** with the message format: `phase <N>: <short summary>`.

### How to resume after a break

- `git log --oneline` to see which phase was last committed.
- Re-open this plan, find the next phase, start a new Sonnet session with the protocol above.

---

## Context — Why this project exists

Maharshi is building a **self-hosted personal AI assistant** as his B.Tech Project (BTP). The machine that runs it is his own laptop; the primary way he interacts with it is from his phone on college WiFi by typing the laptop's LAN IP into a browser. The system is single-user (only Maharshi uses it) and the login exists only to keep random LAN users out.

The BTP's core contribution is a **dynamic multi-agent orchestration system** (built on LangGraph) that gives the assistant "hands" on the laptop — real access to the filesystem, terminal, Gmail, Google Drive, and Google Calendar — with a **safety-first permission model** that routes sensitive actions through phone approval.

The assistant must also support **automations**: natural-language "if X then Y" and "every X do Y" rules that reuse the same agent runtime so anything the user can do interactively in chat, the system can do automatically on a schedule or on an event.

### Design principles (these are non-negotiable)

1. **Single process, single machine, single user.** No distributed anything.
2. **LangGraph is the agent runtime.** Use its built-in streaming, checkpointing, and interrupt features rather than reinventing them.
3. **Everything flows through the graph.** Automations, chat messages, and retries all become invocations of the same LangGraph supervisor. No parallel "automation runtime" living outside the graph.
4. **Workspace + allowlists for safety.** Safety is enforced by policies on tool arguments, not by asking an LLM whether something is safe.
5. **YAGNI hard.** If a feature isn't listed in a phase, it isn't in scope yet.

---

## Final architecture (locked)

### High-level shape

```
                    ┌─────────────────────────────────────────┐
                    │        Laptop (single Python process)   │
┌──────────┐        │                                         │
│  Phone   │──HTTP──┤  FastAPI                                │
│ (browser)│  +WS   │   ├── /  → UI (auto-detect phone/desk)  │
└──────────┘        │   ├── /api/* (chat, automations, ...)   │
                    │   └── /ws  → live agent status          │
┌──────────┐        │                                         │
│ Laptop   │──HTTP──┤  LangGraph supervisor (async)           │
│ browser  │  +WS   │   └─ spawns worker agents in parallel   │
└──────────┘        │      ├─ interrupt() on sensitive tools  │
                    │      └─ streams events to WS hub        │
                    │                                         │
                    │  SQLite (single file)                   │
                    │  APScheduler (time jobs + Gmail poll)   │
                    │  watchdog (filesystem triggers)         │
                    └─────────────────────────────────────────┘
```

### Tech stack (locked — do not substitute)

| Concern                | Choice                          | Why                                                       |
|------------------------|---------------------------------|-----------------------------------------------------------|
| Language               | Python 3.11+                    | LangGraph is Python-first                                 |
| Web framework          | FastAPI                         | Async, WebSocket-native, clean DI                         |
| Agent runtime          | LangGraph                       | Stateful graph, streaming, interrupts, checkpointer       |
| LLM provider           | OpenAI (GPT family)             | User has only this API key                                |
| DB                     | SQLite (via SQLAlchemy)         | Single-user, single-machine — sufficient                  |
| Agent state persistence| LangGraph SqliteSaver           | Built-in, survives restarts                               |
| Scheduling             | APScheduler                     | In-process, persists to SQLite, no broker                 |
| Filesystem events      | `watchdog`                      | OS-native events, cross-platform                          |
| Auth                   | bcrypt + signed session cookie  | Single user, simplest thing that works                    |
| Frontend               | Vanilla HTML + HTMX + Tailwind  | No build step; phone and desktop both render cleanly      |
| Live updates           | FastAPI WebSockets              | One hub broadcasts LangGraph events to connected clients  |
| Google APIs            | `google-api-python-client`      | Official SDK                                              |
| Secrets                | `.env` via `python-dotenv`      | Simplest                                                  |

### Data model (SQLite tables)

- `users` — single row; `username`, `password_hash`
- `sessions` — `token`, `created_at`, `expires_at`
- `threads` — chat conversations; `id`, `title`, `created_at`, `model`
- `messages` — `id`, `thread_id`, `role`, `content`, `created_at`, `metadata_json`
- `langgraph_checkpoints` — managed by LangGraph's SqliteSaver
- `automations` — `id`, `name`, `trigger_type`, `trigger_config_json`, `action_prompt`, `enabled`, `last_run_at`, `created_at`
- `automation_runs` — `id`, `automation_id`, `started_at`, `finished_at`, `status`, `thread_id`
- `permission_audit` — `id`, `tool_name`, `args_json`, `decision` (auto/approved/denied), `decided_by` (policy/user), `decided_at`, `thread_id`
- `oauth_tokens` — `provider` (google), `token_json` (encrypted at rest with Fernet), `refreshed_at`

### Permission model (locked)

- **Workspace directory** chosen at setup (e.g., `E:\BTP-assistant-workspace\`). Reads/writes/lists inside → auto. Anything outside → ask.
- **Deletes and overwrites of existing files** → always ask, even inside workspace.
- **Shell commands:** allowlist auto (`ls`, `dir`, `cat`, `type`, `git status`, `git log`, `git diff`, `python --version`, etc.). Everything else → ask. **Allowlist only, never denylist.**
- **Network reads** (web search, web fetch, Gmail/Drive/Calendar reads) → auto.
- **Network writes** (send email, create/modify Drive files, create calendar events) → always ask.
- **LangGraph `interrupt()`** is used to pause the graph on "ask" decisions; the server pushes a WebSocket notification, the user taps approve/deny on the phone, the server resumes the graph with the decision.
- **Every decision is logged** in `permission_audit` regardless of outcome.
- **Automations inherit the same policies.** An automation firing at 9am that wants to send an email will still trigger a phone approval prompt. No silent dangerous actions.

### Agent architecture (locked)

- **Hierarchical supervisor + workers** on LangGraph.
- **Top-level supervisor:** receives the user's message, decides to (a) handle it directly with tools, or (b) decompose into subtasks and spawn worker agents.
- **Workers:** each worker is itself a ReAct-style agent with tool access. Workers may request more sub-workers via the supervisor (not directly) — bounded.
- **Bounds (hardcoded, non-negotiable):**
  - Max recursion depth: **3**
  - Max total agents per user task: **10**
  - Max total tool calls per user task: **50**
  - Max wall-clock per user task: **10 minutes**
- **Every agent is a node in the same graph.** No agents run "outside" the graph; this is what makes the observability panel universal.
- **Streaming:** the server subscribes to LangGraph's event stream and forwards node-start / node-end / tool-call / tool-result events to a WebSocket hub. The phone/desktop UI renders a live "agent roster" panel from these events.

### Automations model (locked)

- **One table, two trigger kinds:** `trigger_type` is either `cron` (time-based) or `event` (Gmail poll, filesystem watch).
- **At creation time**, one LLM call parses the natural-language rule into `{trigger_type, trigger_config, action_prompt}` which is stored verbatim.
- **APScheduler** runs all cron jobs and also runs Gmail poll loops (every 2 minutes per watched sender) as scheduled background tasks.
- **watchdog** runs filesystem watchers.
- **When a trigger fires**, the `action_prompt` is fed to the LangGraph supervisor as if the user had typed it, creating a new thread tagged `automation_run`. The permission system still applies.

### Scope of v1 features (locked)

**In scope:**
- Single-user login (username + password, bcrypt, session cookie)
- ChatGPT-style threaded chat UI, phone + desktop layouts from one codebase
- All chat history persisted forever, searchable from sidebar
- Multi-agent supervisor + worker orchestration on LangGraph (dynamic graph, bounded)
- Tools: filesystem (read/write/list/delete, workspace-aware), shell (allowlist-aware), web search, web fetch, Gmail (read/send), Drive (read/write), Calendar (read/create)
- Permission system with phone approval for sensitive ops
- Live agent status panel (status cards, not log streams)
- Model switcher (GPT-4o, GPT-4o-mini, GPT-4-turbo, configurable list)
- Natural-language automations (cron + Gmail-new-from-sender + filesystem watch)
- Permission audit log viewable in UI

**Explicitly out of scope for v1** (revisit only after v1 is fully working):
- Voice input
- TOTP / 2FA
- Multi-user support
- Gmail push notifications (Pub/Sub) — we use polling
- Internet exposure / tunneling — LAN only
- Remote access from outside college WiFi
- Any non-OpenAI model providers
- Docker / containerization
- Mobile native app — browser only

---

## Phases

Each phase is self-contained and ends in a committable, runnable state. Do one per session. Verify before moving on.

---

### Phase 0 — Project skeleton & dev loop

**Goal:** A runnable empty FastAPI app, a working SQLite connection, and a clean directory layout. No AI yet.

**Deliverables:**
- `pyproject.toml` or `requirements.txt` pinning: `fastapi`, `uvicorn[standard]`, `sqlalchemy`, `aiosqlite`, `python-dotenv`, `bcrypt`, `itsdangerous`, `jinja2`, `python-multipart`.
- Directory layout:
  ```
  E:\BTP project\
    app\
      __init__.py
      main.py              # FastAPI app entrypoint
      config.py            # settings from .env
      db\
        __init__.py
        engine.py          # SQLAlchemy async engine
        models.py          # empty for now
      web\
        __init__.py
        routes\
          __init__.py
          health.py        # GET /health → {"status": "ok"}
        templates\
          base.html
          index.html       # "Hello from BTP assistant"
        static\
    .env.example
    .gitignore
    README.md              # one-paragraph description + run command
  ```
- `GET /` renders `index.html`.
- `GET /health` returns `{"status": "ok"}`.
- `python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` starts the server.

**Verification:**
- `curl http://localhost:8000/health` → `{"status":"ok"}`
- Open `http://localhost:8000/` in a browser → renders the index page.
- From Maharshi's phone on the same WiFi, visit `http://<laptop-lan-ip>:8000/` → same page loads.
- `git status` clean after commit.

**Commit:** `phase 0: project skeleton and dev loop`

---

### Phase 1 — Auth + single-user login gate

**Goal:** Nobody reaches the app without logging in. One user, bcrypt-hashed password from `.env`.

**Deliverables:**
- `users` and `sessions` tables created via SQLAlchemy migrations (or `Base.metadata.create_all` for simplicity).
- On first startup: if no user exists, read `ADMIN_USERNAME` and `ADMIN_PASSWORD` from `.env`, hash with bcrypt, insert.
- `GET /login` → login form.
- `POST /login` → verifies, issues signed session cookie via `itsdangerous`, redirects to `/`.
- `POST /logout` → clears cookie.
- FastAPI dependency `require_user()` that reads the cookie, validates, loads user, or redirects to `/login`.
- All routes except `/login` and `/health` require auth.

**Verification:**
- `curl http://localhost:8000/` without cookie → 302 to `/login`.
- Login with correct creds → redirected to `/`, now reachable.
- Login with wrong creds → error message on login page.
- Cookie signed; tampering invalidates session.
- Restart server; cookie still valid until expiry.

**Commit:** `phase 1: single-user login with bcrypt + session cookies`

---

### Phase 2 — Chat UI shell (no AI yet)

**Goal:** A ChatGPT-style interface that works on phone and desktop, with threads persisted to SQLite, but the "assistant" responses are stubbed (echoes).

**Deliverables:**
- `threads` and `messages` tables.
- `GET /` → chat UI. Sidebar lists threads; main pane shows active thread.
- `POST /api/threads` → creates a new thread.
- `GET /api/threads/:id/messages` → returns messages.
- `POST /api/threads/:id/messages` → saves user message, generates a stub "echo" assistant reply, returns both.
- UI is built with HTMX + Tailwind. Mobile-responsive: sidebar collapses on narrow screens.
- Model switcher dropdown (displayed but non-functional — stores selection on thread only).
- History: infinite retention, no auto-cleanup.

**Verification:**
- Create a thread on desktop, send a message, see echo reply, refresh page, thread + messages still there.
- Open same URL from phone, see the thread in sidebar, can send messages.
- Second thread can be created and switched between without mixing messages.
- DB row count matches UI.

**Commit:** `phase 2: chat UI with threaded history (stub responses)`

---

### Phase 3 — Single LLM call behind the chat (no agents yet)

**Goal:** Chat actually talks to OpenAI. One user message → one OpenAI call → streamed response. No tools, no agents. This is the smallest possible thing that proves the LLM loop works end-to-end.

**Deliverables:**
- `OPENAI_API_KEY` in `.env`.
- `POST /api/threads/:id/messages` now calls OpenAI (model chosen from thread's `model` field) with the full message history for that thread.
- Response is **streamed** back to the client over a WebSocket or SSE (pick WebSocket — we'll reuse the connection for agent events later).
- UI shows tokens appearing live.
- Model switcher actually switches models on the thread.

**Verification:**
- Send "hello" → receive a real GPT reply, streamed.
- Send "what did I just say?" in the same thread → model sees history, answers correctly.
- Switch model mid-conversation → next reply uses the new model.
- Invalid API key → clear error in UI, no crash.

**Commit:** `phase 3: streaming OpenAI chat over websockets`

---

### Phase 4 — LangGraph supervisor (single agent, no tools)

**Goal:** Replace the direct OpenAI call with a LangGraph graph. Still one agent, still no tools — but now the runtime is LangGraph and we're streaming **node events** instead of raw tokens.

**Deliverables:**
- Add `langgraph`, `langchain-openai` to deps.
- `app/agents/supervisor.py` defines a minimal LangGraph StateGraph with one node (`chat`) that calls OpenAI.
- `SqliteSaver` checkpointer wired to the same SQLite file.
- The chat endpoint now invokes `graph.astream_events(...)` and forwards events to the WebSocket.
- UI now has a **collapsed** "agents" panel that shows "supervisor — thinking..." → "supervisor — done" based on node events. It's minimal for now but plumbed end-to-end.

**Verification:**
- Chat still works identically from the user's perspective.
- The agents panel shows the supervisor node entering and exiting on each turn.
- Kill the server mid-response → restart → the checkpoint is there in SQLite (even if the in-flight generation is lost).

**Commit:** `phase 4: langgraph supervisor with streaming events`

---

### Phase 5 — Tools: filesystem + shell + web (workspace-aware, no permissions yet)

**Goal:** Give the supervisor real tools. Workspace directory is respected. **No permission prompts yet** — everything inside workspace is auto, everything outside just errors. We add real permission gating in Phase 6.

**Deliverables:**
- `WORKSPACE_DIR` in `.env`, created on startup if missing.
- `app/tools/filesystem.py`: `read_file`, `write_file`, `list_dir`, `delete_file`. All take paths and raise `OutsideWorkspaceError` if path is outside workspace.
- `app/tools/shell.py`: `run_shell_command` with allowlist (`ls`, `dir`, `cat`, `type`, `git status`, `git log`, `git diff`, `python --version`, `pwd`, `whoami`, `echo`). Anything else raises `NotAllowlistedError`.
- `app/tools/web.py`: `web_search` (use DuckDuckGo via `ddgs` library — no API key needed), `web_fetch` (httpx + readability).
- Tools are registered with LangGraph via the standard `ToolNode` pattern. Supervisor becomes a ReAct agent that can call them.
- Agents panel now shows tool calls as they happen: "supervisor → list_dir(./) → done".

**Verification:**
- "List the files in my workspace" → correct output.
- "Read the contents of README.md in my workspace" → correct output.
- "Delete all files" → tool errors; assistant reports the error (no prompt yet, that's next phase).
- "Run `rm -rf /`" → blocked by allowlist, assistant explains.
- "Search the web for langgraph docs" → returns results.
- Agents panel shows each tool call and its outcome.

**Commit:** `phase 5: workspace-aware tools (fs, shell allowlist, web)`

---

### Phase 6 — Permission system with phone approval

**Goal:** Sensitive tool calls pause the graph via LangGraph `interrupt()`, push a notification over WebSocket, and wait for approval.

**Deliverables:**
- `app/permissions/policy.py`: one function per tool returning `"auto" | "ask"` based on args.
- Wrap every tool so that before execution it consults the policy.
- If "ask": call LangGraph `interrupt({"tool": ..., "args": ..., "prompt": "..."})`. The graph pauses and checkpoints.
- Server catches the interrupt, pushes a WebSocket message to the active client: `{type: "permission_request", id, tool, args, prompt}`.
- UI (phone and desktop) renders an inline Approve / Deny card in the chat.
- On approve/deny, UI posts to `POST /api/permissions/:id` with the decision. Server resumes the graph via `graph.ainvoke(None, config={...})` passing the decision.
- Every request + decision logged to `permission_audit`.
- Hardcoded policies (exactly as specified in the Permission Model section above).

**Verification:**
- "Delete README.md from workspace" → approval card appears on phone; tap approve → file deleted; tap deny → assistant reports denial.
- Refresh the phone mid-approval → upon reconnect, the pending request is re-sent (from the checkpoint).
- "Run `rm -rf .`" → still blocked by allowlist before policy even fires (defense in depth).
- `permission_audit` table has rows for every decision.

**Commit:** `phase 6: permission system with websocket approval flow`

---

### Phase 7 — Dynamic multi-agent orchestration (supervisor + workers)

**Goal:** Supervisor can decompose a task and spawn parallel worker agents. Workers run concurrently. Bounds enforced.

**Deliverables:**
- Supervisor node gains a `spawn_worker(task_description, tools_allowed)` tool.
- A separate `worker` subgraph: a ReAct agent with its own tool set, runs as a LangGraph subgraph invocation.
- Supervisor runs workers in **parallel** via `asyncio.gather`, each as a separate LangGraph subgraph invocation. Workers stream their own node events.
- Recursion depth, total-agent, total-tool-call, and wall-clock bounds enforced in a central `RunContext` object passed through graph state.
- Agents panel now shows a tree: supervisor with worker children, each with status (queued / running / done / failed), a one-line description of what they're doing, and a tool-call count.
- If a worker wants to spawn its own sub-worker, it must request it via the supervisor (not directly).

**Verification:**
- "Search the web for three Python web frameworks, summarize each, and write a comparison to `comparison.md` in my workspace." → supervisor spawns 3+ workers in parallel (visible in panel) → comparison file written → approval prompted for the write.
- Bounds: a prompt engineered to blow past the 10-agent cap → stops at 10 with a clean error in the chat.
- Wall-clock: 10-minute timeout enforced (test with a short override).
- Killing the server mid-run → restart → graph state is checkpointed, but the in-flight run is reported as failed in the UI (this is fine for v1).

**Commit:** `phase 7: dynamic multi-agent orchestration with bounds`

---

### Phase 8 — Google integrations (Gmail, Drive, Calendar)

**Goal:** Real OAuth flow to connect Maharshi's Google account, then Gmail/Drive/Calendar tools available to agents.

**Deliverables:**
- `GET /settings/google/connect` → starts Google OAuth flow (desktop app credentials, localhost redirect on port 8000).
- Scopes requested: `gmail.readonly`, `gmail.send`, `drive`, `calendar`.
- Tokens stored encrypted (Fernet) in `oauth_tokens` table.
- Tools:
  - `gmail_list_unread(max)`, `gmail_read(message_id)`, `gmail_search(query)`, `gmail_send(to, subject, body)` — send is always "ask".
  - `drive_list(folder_id)`, `drive_read(file_id)`, `drive_write(folder_id, name, content)` — write is always "ask".
  - `calendar_list_events(time_range)`, `calendar_create_event(...)` — create is always "ask".
- Settings page shows connection status + "disconnect" button.

**Verification:**
- Connect Google account → tokens saved → `/settings` shows "connected".
- "Summarize my 5 most recent unread emails" → works without approval (read-only).
- "Send a test email to me saying hello" → approval prompt → approve → email arrives.
- "What's on my calendar tomorrow?" → works.
- Disconnect → tools fail with a clear message instructing the user to reconnect.

**Commit:** `phase 8: google oauth + gmail/drive/calendar tools`

---

### Phase 9 — Automations (cron + gmail-from-sender + filesystem watch)

**Goal:** Natural-language automations that route back through the supervisor.

**Deliverables:**
- `automations` and `automation_runs` tables.
- `app/automations/parser.py`: one OpenAI call, structured-output, that converts NL → `{trigger_type, trigger_config, action_prompt}`. Supported `trigger_type`:
  - `cron` (config: cron expression or preset like "every monday 9am")
  - `gmail_new_from_sender` (config: `{sender}`)
  - `fs_new_in_folder` (config: `{folder}`)
- `app/automations/runtime.py`:
  - Registers all `cron` automations with APScheduler on startup.
  - Registers one APScheduler poll loop per `gmail_new_from_sender` automation (2-minute interval); tracks `last_seen_message_id` per sender.
  - Registers a `watchdog` observer per `fs_new_in_folder` folder.
- When a trigger fires: creates a new thread tagged `automation_run=<id>`, injects `action_prompt` as the first message, runs it through the supervisor exactly like a user message. Permission policies still apply.
- `/automations` UI: list, create (NL input), enable/disable, delete, and view recent runs (each run links to its thread so Maharshi can see what happened).

**Verification:**
- Create: "Every minute, write the current time to `heartbeat.txt` in my workspace." → runs → file updated every minute. (Use 1-minute interval for testing, then disable.)
- Create: "When I get a new email from maharshinahar10@gmail.com, draft a reply and save it as a .txt file in my workspace." → send a test email from that address → within ~2 min, a new thread appears with the automation run, a draft file is created (after approval).
- Create: "When a new file is added to `<workspace>/inbox`, summarize it." → drop a file → automation fires → summary appears in a thread.
- Disable an automation → it stops firing immediately.
- Server restart → all automations reload from DB and resume.

**Commit:** `phase 9: natural-language automations (cron, gmail poll, fs watch)`

---

### Phase 10 — Polish, observability panel, and audit viewer

**Goal:** Make it demo-ready for the viva. No new features; only clarity.

**Deliverables:**
- Agents panel refined: tree view with colored status badges, collapsible workers, current-action text ("searching web for 'x'", "writing comparison.md"), tool-call counter, wall-clock. Live updates smoothly via WebSocket.
- Permission audit viewer at `/audit`: paginated table of every auto/approved/denied decision with tool name, args (truncated), timestamp, linked thread.
- Settings page: model list (editable from UI), workspace directory (read-only display), Google connection status, change-password form.
- Error handling pass: every tool wraps exceptions into user-friendly error messages; no Python tracebacks reach the UI.
- README updated with: project overview, architecture diagram (ASCII), setup instructions, run command, Google OAuth setup steps, screenshots.
- `seed_demo.py` script that creates a couple of sample threads and automations for demo day.

**Verification:**
- Cold-run the setup from the README on a clean checkout — does it work?
- Demo scenario 1: on phone, ask "search the web for three langgraph tutorials, summarize them, and save the summary to my workspace." Panel shows 3 workers in parallel. Save is approved from the phone. Works end-to-end.
- Demo scenario 2: create a cron automation "every minute write the time to heartbeat.txt", watch it run twice, disable it.
- Demo scenario 3: open audit viewer, show the history of approvals from the above runs.
- From a different device on the LAN without the cookie → blocked at `/login`.

**Commit:** `phase 10: polish, observability, audit viewer, docs`

---

## Post-v1 backlog (not in scope — do not build until v1 ships)

- Voice input via Web Speech API on phone
- TOTP 2FA
- Gmail Pub/Sub push notifications (eliminates 2-minute poll latency)
- Anthropic / local model support
- More trigger types (calendar event starting, time-of-day + condition, RSS feeds)
- Per-automation "pre-approve its own actions" opt-in
- Usage + cost dashboard (per-model token counts)
- Multi-workspace support

---

## Critical files reference

When executing any phase, these files are the anchors Sonnet should know about:

- `app/main.py` — FastAPI app + startup/shutdown hooks for APScheduler, watchdog, LangGraph
- `app/config.py` — all env-driven settings
- `app/db/models.py` — SQLAlchemy models for every table listed in the data model section
- `app/agents/supervisor.py` — LangGraph StateGraph definition for supervisor + workers
- `app/tools/` — one file per tool family; every tool has a policy
- `app/permissions/policy.py` — the one place that decides auto vs ask
- `app/automations/runtime.py` — APScheduler + watchdog setup
- `app/web/routes/` — one file per route group (auth, chat, automations, audit, settings)
- `app/web/templates/` — Jinja templates, one layout + per-page partials
- `.env.example` — documents every required env var

---

## Final reminders for Sonnet

- **Never run destructive git commands** (`reset --hard`, `push --force`, etc.) without explicit approval.
- **Never commit `.env`.** Only `.env.example`.
- **Never commit the SQLite DB file.** Add `*.db`, `*.sqlite`, `*.sqlite3` to `.gitignore` in phase 0.
- **Never store API keys, OAuth tokens, or passwords in code.** Env or encrypted DB only.
- **Verification is not optional.** Every phase ends with verification output pasted to Maharshi before a commit.
- **Scope creep is the #1 risk.** If you find yourself wanting to "also add X while I'm here" — don't. Write it in a TODO comment and move on.
