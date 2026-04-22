# RAION

A self-hosted personal AI assistant you run on your own machine. Chat with it from any browser, automate tasks, monitor WhatsApp groups, connect to external services, and let it act on your behalf — all without sending your data to a third-party platform.

---

## What it does

- **Chat** — ChatGPT-style threaded conversations with streaming responses, powered by OpenAI models (GPT-4o, GPT-4o-mini, etc.)
- **Multi-agent** — supervisor spawns parallel worker agents for complex tasks; bounded by depth, agent count, tool calls, and wall-clock time
- **WhatsApp** — connects via Green API; polls groups every 30 seconds, stores all messages, supports automations triggered by incoming messages, smart auto-replies, and scheduled summaries
- **Telegram** — bidirectional; send notifications, ask questions, receive file uploads, run slash commands from your phone
- **Automations** — describe rules in plain English ("every day at 9pm summarize my WhatsApp groups and send me a Telegram report"); supports cron, Gmail triggers, filesystem watch, and WhatsApp message triggers
- **Google** — Gmail read/send, Drive list/read/write/upload/download, Calendar list/create via OAuth2
- **MCP Connectors** — connect any MCP-compatible server (Notion, GitHub, Slack, Linear, Postgres, etc.) and use its tools from chat
- **Memory** — store facts about yourself; auto-extracted from conversations; injected into every agent run
- **Skills** — upload markdown skill files; agent loads them on demand; reusable procedures for recurring tasks
- **Filesystem** — workspace-scoped read/write/delete/move/copy/search tools
- **Shell** — run shell commands (allowlist-gated)
- **Web** — DuckDuckGo search, HTTP fetch
- **Image generation** — DALL-E image generation from chat
- **RAG** — ingest files into a local vector store, search by meaning across your workspace
- **Permission system** — sensitive actions pause and show an approval card in the UI; every decision logged to `/audit`
- **Dark mode** — full dark theme, persisted in localStorage

---

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Web framework | FastAPI |
| Agent runtime | LangGraph (StateGraph, streaming, interrupt/resume) |
| LLM provider | OpenAI (GPT-4o / GPT-4o-mini) |
| Database | SQLite via SQLAlchemy async + aiosqlite |
| Scheduling | APScheduler (cron + interval jobs) |
| Filesystem events | watchdog |
| WhatsApp | Green API |
| Telegram | Bot API (webhook) |
| MCP | `mcp` SDK + `langchain-mcp-adapters` |
| Auth | bcrypt + itsdangerous session cookies |
| Frontend | Jinja2 + HTMX + Tailwind CSS (offline) |
| Live updates | FastAPI WebSockets |
| Encryption | Fernet (MCP env vars, OAuth tokens) |

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/Maharshi-n/btp-assistant.git
cd btp-assistant
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create `.env`

Create a `.env` file in the project root:

```env
SECRET_KEY=                        # generate: python -c "import secrets; print(secrets.token_hex(32))"
DATABASE_URL=sqlite+aiosqlite:///./app.db
ADMIN_USERNAME=admin
ADMIN_PASSWORD=yourpassword
OPENAI_API_KEY=sk-...
WORKSPACE_DIR=./workspace

# Telegram (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_WEBHOOK_URL=              # your public HTTPS URL, e.g. ngrok
TELEGRAM_WEBHOOK_SECRET=

# WhatsApp via Green API (optional)
GREEN_API_INSTANCE_ID=
GREEN_API_TOKEN=
GREEN_API_BASE_URL=https://api.green-api.com
GREEN_API_WEBHOOK_TOKEN=

# Google OAuth (optional)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Encryption key for tokens and MCP env vars at rest
FERNET_KEY=                        # generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 4. Run

```bash
python run.py
```

Opens at `http://localhost:8000`

---

## WhatsApp setup

1. Create a free account at [green-api.com](https://green-api.com), create an instance, scan the QR code with your WhatsApp
2. Add your `GREEN_API_INSTANCE_ID` and `GREEN_API_TOKEN` to `.env`
3. Start RAION, go to `/whatsapp`, add your groups
4. Run ngrok: `ngrok http 8000`, paste the URL in the Webhook URL box, click Save & Apply
5. RAION polls all registered groups every 30 seconds — messages are stored locally and trigger automations

---

## Telegram setup

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token
2. Get your chat ID (send a message to your bot, check `https://api.telegram.org/bot<token>/getUpdates`)
3. Fill `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_URL`, `TELEGRAM_WEBHOOK_SECRET` in `.env`
4. Restart RAION — webhook is registered automatically on startup

---

## Google OAuth setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/), enable Gmail, Drive, and Calendar APIs
2. Create an OAuth client ID — select **Desktop app** type
3. Copy `client_id` and `client_secret` into `.env`
4. Go to Settings → Connect Google in RAION and complete the OAuth flow

---

## Directory layout

```
app/
  main.py                  # FastAPI app, startup/shutdown
  config.py                # env var loading
  agents/
    supervisor.py          # LangGraph multi-agent graph
    auto_memory.py         # auto-extract facts from conversations
  automations/
    parser.py              # NL → structured automation spec
    runtime.py             # APScheduler + WhatsApp polling
    conversations.py       # multi-round Telegram conversation state
  mcp/
    manager.py             # MCP session manager
    loader.py              # TTL-cached tool loader
    crypto.py              # Fernet encrypt/decrypt
  permissions/
    policy.py              # auto vs ask per tool
  tools/
    filesystem.py          # read, write, list, delete, move, copy
    shell.py               # run_shell_command
    web.py                 # web_search, web_fetch
    google_tools.py        # Gmail, Drive, Calendar
    telegram_tools.py      # telegram_send, telegram_ask, save_draft
    whatsapp_tools.py      # whatsapp_send, whatsapp_fetch_messages, etc.
    image.py               # DALL-E image generation
    skills.py              # read_skill
    rag.py                 # rag_ingest, rag_search
  db/
    engine.py              # SQLAlchemy async engine
    models.py              # all table definitions
    seed.py                # admin user + workspace seeding
  web/
    routes/                # FastAPI routers (chat, ws, automations, whatsapp, ...)
    templates/             # Jinja2 HTML templates
    static/                # Tailwind CSS + HTMX (offline copies)
run.py                     # entry point
requirements.txt
.env.example
```

---

## Permission model

| Operation | Decision |
|---|---|
| Read / list inside workspace | Auto |
| Write new file | Auto |
| Overwrite existing file | Ask |
| Delete any file | Ask |
| Shell commands (allowlisted) | Auto |
| Any other shell command | Ask |
| Web search / fetch | Auto |
| Gmail send / Drive write / Calendar create | Ask |
| WhatsApp send | Ask |
| WhatsApp read / fetch | Auto |
| MCP tools | Ask (configurable per tool) |
| Unknown tool | Ask (safe default) |

Every decision is logged and viewable at `/audit`.
