# Design: Custom Telegram Commands

**Date:** 2026-04-16  
**Project:** RAION  
**Status:** Approved

---

## Overview

Allow users to define custom Telegram slash commands (e.g. `/standup`, `/summary`) from the RAION web UI. When sent from Telegram, a command creates a fresh thread and runs the agent with an optional preset prompt — no web UI thread required to start. Commands are distinct from Skills: Skills inject context into the agent during a chat session; Commands are Telegram-only action shortcuts that bypass the thread UI entirely.

---

## 1. Data Model

New table: `telegram_commands` in `app/db/models.py`.

```python
class TelegramCommand(Base):
    __tablename__ = "telegram_commands"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # shown in /help output
    description: Mapped[str] = mapped_column(String(256), nullable=False)
    # optional preset system instruction
    preset_prompt: Mapped[str] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT, server_default=func.now(), nullable=False)
```

- `name` stores the slug without the leading slash (e.g. `standup`)
- No relation to the `Skill` table — entirely separate concept

---

## 2. Telegram Webhook Routing

Location: `app/web/routes/telegram.py`

### Placement in the dispatch chain

Custom commands are checked **after** all built-in commands (`/newthread`, `/thread`, `/model`, `/help`, `/remember`, `/ls`, `/remind`) and **before** the pending-reply lookup. This means built-in names can never be shadowed by custom commands.

### Dispatch logic

```
1. text starts with "/" and name matches an enabled TelegramCommand row
2. if pending reply exists for chat_id → reply "Finish your current conversation first." → return
3. extract user_extra = text after the command name (stripped)
4. build prompt:
     - preset + user_extra → "<preset_prompt>. User added: <user_extra>"
     - preset only          → preset_prompt
     - user_extra only      → user_extra
     - neither              → reply "Please provide a prompt or set a default one in the web UI." → return
5. create new Thread in DB
6. send "Got it, working on it..." to Telegram
7. asyncio.create_task(_run_direct_thread(prompt, thread_id))
8. after agent finishes: send result via Telegram + _register_pending_reply(chat_id, thread_id)
```

### File attachments

No special handling needed. The existing file accumulation logic runs before text routing. If a user sends a file with a caption like `/standup focus on backend`, the caption is treated as the command text and the file is injected via `file_context` exactly as with regular messages.

---

## 3. Web UI

### New route file: `app/web/routes/telegram_commands.py`

| Method | Path | Action |
|--------|------|--------|
| GET | `/telegram-commands` | HTML page listing all commands |
| POST | `/api/telegram-commands` | Create a new command |
| POST | `/api/telegram-commands/:id/enable` | Enable a command |
| POST | `/api/telegram-commands/:id/disable` | Disable a command |
| DELETE | `/api/telegram-commands/:id` | Delete a command |

### New template: `app/web/templates/telegram_commands.html`

- Extends `base.html`, dark-theme consistent with existing pages
- **List**: table with columns: name (with `/` prefix shown), description, preset prompt (truncated to ~60 chars), enabled toggle, delete button
- **Create form**: `name` (text input), `description` (text input), `preset_prompt` (textarea, optional)
- No edit — delete and recreate (consistent with skills/automations pattern)

### /help update

The existing `/help` handler in `telegram.py` queries enabled `TelegramCommand` rows and appends them to the reply:

```
Custom commands:
/standup — Daily standup summary
/summary — Summarize today's tasks
```

### Registration

Router registered in `app/main.py` alongside existing routers.

---

## 4. Error Handling & Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Name collides with built-in (`help`, `remind`, etc.) | 409 on create with message listing reserved names |
| Duplicate custom name | 409 on create (DB unique constraint) |
| Command called while pending reply exists | Reply: "Finish your current conversation first." — no agent run |
| Bare command with no preset and no user text | Reply: "Please provide a prompt or set a default one in the web UI." — no agent run |
| Disabled command | Falls through as unrecognised text — not treated as a command |
| Invalid name format | Validated on save: lowercase, alphanumeric + underscores only, no leading slash |

### Name validation (on create)

- Strip leading `/` if present
- Lowercase, replace spaces with underscores
- Reject if contains characters other than `[a-z0-9_]`
- Reject if in reserved set: `newthread`, `thread`, `model`, `help`, `remember`, `ls`, `remind`

---

## 5. Files Changed / Created

| File | Change |
|------|--------|
| `app/db/models.py` | Add `TelegramCommand` model |
| `app/web/routes/telegram_commands.py` | New — CRUD routes |
| `app/web/templates/telegram_commands.html` | New — web UI page |
| `app/web/routes/telegram.py` | Add custom command dispatch + update `/help` |
| `app/main.py` | Register new router |

---

## 6. Out of Scope

- Editing commands in-place (delete + recreate is sufficient)
- Creating commands from Telegram itself
- Per-command model selection
- Per-command permission levels
- Rate limiting per command
