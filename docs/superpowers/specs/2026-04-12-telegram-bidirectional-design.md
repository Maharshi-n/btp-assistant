# Telegram Bidirectional Chat — Design Spec
**Date:** 2026-04-12
**Project:** RAION
**Status:** Approved

---

## Summary

Add two-way Telegram communication to automations. When an automation needs user input (e.g. "summarize email → ask me for a reply → send the email"), the supervisor calls `telegram_ask(question, continuation_prompt)` which sends a message to Telegram and stores a pending reply in the DB. When the user replies on Telegram, a webhook endpoint receives it, resumes the automation with the reply text, and runs the supervisor to completion. The ngrok URL is configured in Settings and registered as the Telegram webhook automatically.

---

## Architecture

```
User types reply in Telegram
        │
        ▼
Telegram Bot API
        │  POST (webhook)
        ▼
POST /telegram/webhook   ← new FastAPI endpoint
        │
        ├─ Pending reply exists for this chat_id?
        │       │
        │      YES → delete row → build prompt → run supervisor → send confirmation
        │       │
        │      NO  → return 200 silently
```

---

## Components

### 1. `TelegramPendingReply` DB model (NEW — `app/db/models.py`)

```python
class TelegramPendingReply(Base):
    __tablename__ = "telegram_pending_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    continuation_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

- Only one pending reply per `chat_id` at a time — new one replaces old
- Expires after 24 hours — stale rows are ignored

### 2. `telegram_ask` tool (MODIFIED — `app/tools/telegram_tools.py`)

```python
@tool
async def telegram_ask(question: str, continuation_prompt: str) -> str
```

- Sends `question` to Telegram chat via Bot API
- Creates/replaces a `TelegramPendingReply` row in DB with 24h expiry
- Returns `"Asked. Waiting for your Telegram reply."` to the supervisor
- Supervisor writes `continuation_prompt` containing full context needed to resume

Example supervisor call:
```
telegram_ask(
  question="New email from maharshi@gmail.com: asking about hackathon dates.\n\nWhat should I reply?",
  continuation_prompt="Using the user's reply below, compose a polite email and send it to naharmaharshi@gmail.com via gmail_send."
)
```

### 3. `POST /telegram/webhook` (NEW — `app/web/routes/telegram.py`)

- Validates `X-Telegram-Bot-Api-Secret-Token` header against `TELEGRAM_WEBHOOK_SECRET`
- Extracts `chat_id` and `text` from Telegram update JSON
- Looks up non-expired `TelegramPendingReply` for that `chat_id`
- If found:
  - Deletes the pending reply row
  - Builds full prompt: `continuation_prompt + "\n\nUser's reply: " + text`
  - Sends `"Got it. Working on it..."` via Telegram immediately
  - Runs supervisor (creates thread, runs LangGraph graph) — same pattern as `_fire_automation`
  - When done, sends result summary via `telegram_send`
- If not found: returns HTTP 200 silently (Telegram requires 200 or retries)

### 4. `POST /settings/telegram/webhook` (MODIFIED — `app/web/routes/settings.py`)

- Accepts `{webhook_url}` (just the ngrok base URL, e.g. `https://abc123.ngrok-free.app`)
- Auto-appends `/telegram/webhook` to form the full URL
- Generates `TELEGRAM_WEBHOOK_SECRET` (random 32-char hex) if not already set
- Calls Telegram Bot API `setWebhook` with full URL + secret token
- Saves `TELEGRAM_WEBHOOK_URL` and `TELEGRAM_WEBHOOK_SECRET` to `.env` + live config
- Redirects to `/settings?webhook_ok=1` on success, `?webhook_error=<msg>` on failure

### 5. Settings UI (MODIFIED — `app/web/templates/settings.html`)

Inside the existing Telegram Notifications card, below the Chat ID field:
- "Webhook URL" input (placeholder: `https://abc123.ngrok-free.app`)
- "Register Webhook" button → submits to `POST /settings/telegram/webhook`
- Status line: "✓ Webhook active" (green) if `TELEGRAM_WEBHOOK_URL` is set, "Not registered" (gray) if not
- Flash banners for `webhook_ok` and `webhook_error` query params

### 6. `app/config.py` (MODIFIED)

Two new fields:
```python
TELEGRAM_WEBHOOK_URL: str = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
TELEGRAM_WEBHOOK_SECRET: str = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
```

### 7. `app/main.py` (MODIFIED)

- Import and register `telegram_router` from `app/web/routes/telegram.py`
- On startup: if `TELEGRAM_WEBHOOK_URL` and `TELEGRAM_WEBHOOK_SECRET` are set, re-register the webhook with Telegram (in case it lapsed during ngrok restart)

### 8. `app/agents/supervisor.py` (MODIFIED)

- Import and add `telegram_ask` to `SUPERVISOR_TOOLS` and `WORKER_TOOLS`
- Add to TOOLS AVAILABLE section of system prompt:
  ```
  Telegram   : telegram_send, telegram_ask
  ```

### 9. `app/permissions/policy.py` (MODIFIED)

- Add `policy_telegram_ask(args) → "auto"`

---

## Data Flow — "Summarize email → ask for reply → send"

1. Email arrives from `naharmaharshi@gmail.com`
2. `_gmail_poll` fires → `_fire_automation()` runs
3. Supervisor receives email content + action_prompt
4. Supervisor calls `telegram_ask(question="..summary..\n\nWhat should I reply?", continuation_prompt="...send email to naharmaharshi@gmail.com using user's reply...")`
5. `telegram_ask` sends question to Telegram, stores `TelegramPendingReply` row, returns
6. Automation run finishes (supervisor is done for now)
7. User reads Telegram message, types: "Tell him registration opens Monday"
8. Telegram calls `POST /telegram/webhook` with that text
9. Webhook finds pending reply, deletes it, builds prompt, sends "Got it. Working on it..."
10. Webhook runs supervisor with `continuation_prompt + "\n\nUser's reply: Tell him registration opens Monday"`
11. Supervisor calls `gmail_send(to="naharmaharshi@gmail.com", ...)`
12. Webhook sends confirmation: "✓ Email sent to naharmaharshi@gmail.com"

---

## Parser Update (`app/automations/parser.py`)

Add to `_SYSTEM_PROMPT` a section explaining when to use `telegram_ask` vs `telegram_send`:

```
telegram_send  → one-way notification, no reply needed
telegram_ask   → needs user input before continuing (e.g. "ask me for a reply")
```

---

## Security

- Webhook endpoint validates `X-Telegram-Bot-Api-Secret-Token` header — rejects anything without it with HTTP 403
- Secret is a random 32-char hex string generated once and stored in `.env`
- Pending replies expire after 24h — no stale state accumulates

---

## Constraints & Decisions

- **One pending reply per chat_id** — if a new automation asks before you've replied to the previous one, the old one is replaced. Simple and avoids queue complexity.
- **Webhook re-registration on startup** — ngrok URLs change on restart; startup re-registers automatically if config is present.
- **"Got it. Working on it..." sent immediately** — supervisor can take 30-60s; the instant acknowledgement prevents you from thinking the message was lost.
- **Webhook URL stored as base URL only** — `/telegram/webhook` path is always appended by the settings endpoint, so the user never has to type the path.
- **Draft-first behavior** — if the automation's `action_prompt` says "show me the draft first", the supervisor calls `telegram_ask` again with the draft, waits for confirmation, then sends. This is handled naturally by the supervisor's reasoning — no special code needed.
