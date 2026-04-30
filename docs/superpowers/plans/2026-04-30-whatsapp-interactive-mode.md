# WhatsApp Interactive Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "interactive mode" toggle to WhatsApp groups so messages from that group open a persistent conversation thread (like Telegram) instead of firing fire-and-forget automations.

**Architecture:** A new `interactive_mode` boolean column on `WhatsAppGroup` and a new `WhatsAppPendingThread` DB table track the open thread per chat_id. The webhook handler checks the flag: if interactive mode → route into persistent thread with idle-close + bye/exit detection (mirroring Telegram's pattern exactly); if not → existing fire-and-forget automation path unchanged. A new `_run_wa_interactive` helper in `app/web/routes/whatsapp.py` handles the thread execution, reusing `_run_direct_thread` from `telegram.py` via import.

**Tech Stack:** SQLAlchemy async, APScheduler (idle close), Green API (`whatsapp_send`), FastAPI, existing `_run_direct_thread` from telegram.py.

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `app/db/models.py` | Modify | Add `interactive_mode` to `WhatsAppGroup`; add `WhatsAppPendingThread` model |
| `app/web/routes/whatsapp.py` | Modify | Interactive mode routing in `_handle_incoming`; idle close helpers; bye/exit detection; UI toggle |
| `app/web/templates/whatsapp.html` | Modify | Show interactive mode toggle in group card |

---

### Task 1: DB model changes

**Files:**
- Modify: `app/db/models.py`

- [ ] **Step 1: Read the current WhatsAppGroup model**

Read `app/db/models.py` lines 309–322 to confirm current columns before editing.

- [ ] **Step 2: Add `interactive_mode` column to `WhatsAppGroup`**

In `app/db/models.py`, find the `WhatsAppGroup` class and add one line after `auto_send_allowed`:

```python
    interactive_mode: Mapped[bool] = mapped_column(default=False, nullable=False)
```

- [ ] **Step 3: Add `WhatsAppPendingThread` model**

After the `WhatsAppMessage` class (around line 344), add:

```python
class WhatsAppPendingThread(Base):
    """Tracks an open interactive-mode conversation for a WhatsApp chat_id."""
    __tablename__ = "whatsapp_pending_threads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_DT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT, server_default=func.now(), nullable=False)
```

- [ ] **Step 4: Add migration — create the new table and column**

Run from project root:
```bash
python -c "
import asyncio
from app.db.engine import engine
from app.db.models import Base
from sqlalchemy import text

async def migrate():
    async with engine.begin() as conn:
        # Add interactive_mode column if not exists
        try:
            await conn.execute(text('ALTER TABLE whatsapp_groups ADD COLUMN interactive_mode BOOLEAN NOT NULL DEFAULT FALSE'))
            print('added interactive_mode column')
        except Exception as e:
            print(f'interactive_mode column already exists or error: {e}')
        # Create whatsapp_pending_threads table
        await conn.run_sync(lambda sync_conn: Base.metadata.tables['whatsapp_pending_threads'].create(sync_conn, checkfirst=True))
        print('whatsapp_pending_threads table ready')

asyncio.run(migrate())
"
```
Expected output:
```
added interactive_mode column
whatsapp_pending_threads table ready
```

- [ ] **Step 5: Verify**

```bash
python -c "
import asyncio
from app.db.engine import AsyncSessionLocal
from app.db.models import WhatsAppPendingThread
from sqlalchemy import select
async def check():
    async with AsyncSessionLocal() as db:
        await db.execute(select(WhatsAppPendingThread).limit(1))
        print('ok')
asyncio.run(check())
"
```
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add app/db/models.py
git commit -m "feat: add WhatsApp interactive_mode flag and WhatsAppPendingThread model"
```

---

### Task 2: Interactive mode logic in whatsapp.py

**Files:**
- Modify: `app/web/routes/whatsapp.py`

This task adds all the runtime logic: idle close scheduler, bye/exit detection, pending thread management, and routing in `_handle_incoming`.

- [ ] **Step 1: Read current imports at top of whatsapp.py**

Read `app/web/routes/whatsapp.py` lines 1–35 to see current imports.

- [ ] **Step 2: Add imports**

At the top of `app/web/routes/whatsapp.py`, find the existing imports block and add these if not already present:

```python
from datetime import datetime, timedelta, timezone
from app.db.models import WhatsAppPendingThread
```

- [ ] **Step 3: Add idle-close constants and helpers**

After the existing `_notify_owner_telegram` helper (find it by searching for `def _notify_owner_telegram`), add this block:

```python
# ---------------------------------------------------------------------------
# Interactive mode — idle close + bye/exit detection
# ---------------------------------------------------------------------------

_WA_IDLE_TIMEOUT_SECONDS = 120
_WA_END_PHRASES = {
    "no", "nope", "nah", "nothing", "nothing else", "that's all", "thats all",
    "that's it", "thats it", "done", "bye", "goodbye", "thanks", "thank you",
    "ok thanks", "ok thank you", "no thanks", "no thank you", "all good",
    "i'm good", "im good", "stop", "exit", "quit", "end",
}


def _wa_is_end_reply(text: str) -> bool:
    return text.lower().strip().rstrip("!.") in _WA_END_PHRASES


def _wa_idle_job_id(chat_id: str) -> str:
    return f"wa_idle_{chat_id}"


def _wa_schedule_idle_close(chat_id: str, thread_id: int) -> None:
    """Schedule (or reschedule) a 2-min idle-close job for this WhatsApp chat."""
    try:
        from app.automations.runtime import get_scheduler
        from apscheduler.triggers.date import DateTrigger

        scheduler = get_scheduler()
        if scheduler is None or not scheduler.running:
            return
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=_WA_IDLE_TIMEOUT_SECONDS)
        scheduler.add_job(
            _wa_fire_idle_close,
            trigger=DateTrigger(run_date=fire_at),
            id=_wa_idle_job_id(chat_id),
            args=[chat_id, thread_id],
            replace_existing=True,
            max_instances=1,
        )
    except Exception as exc:
        logger.warning("_wa_schedule_idle_close: failed: %s", exc)


def _wa_cancel_idle_close(chat_id: str) -> None:
    try:
        from app.automations.runtime import get_scheduler
        scheduler = get_scheduler()
        if scheduler is None or not scheduler.running:
            return
        job = scheduler.get_job(_wa_idle_job_id(chat_id))
        if job:
            job.remove()
    except Exception as exc:
        logger.warning("_wa_cancel_idle_close: failed: %s", exc)


async def _wa_fire_idle_close(chat_id: str, thread_id: int) -> None:
    """APScheduler job: clear pending thread and notify user via WhatsApp."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
            )
            pending = result.scalars().first()
            if pending is None or pending.thread_id != thread_id:
                return
            if pending.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                return
            await db.execute(
                delete(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
            )
            await db.commit()
    except Exception as exc:
        logger.warning("_wa_fire_idle_close: db error: %s", exc)
        return

    try:
        from app.integrations.green_api import get_green_client
        client = get_green_client()
        if client:
            await client.send_message(chat_id, "Thread closed due to inactivity. Send a message anytime to start a new one.")
    except Exception as exc:
        logger.warning("_wa_fire_idle_close: send failed: %s", exc)

    logger.info("_wa_fire_idle_close: closed idle thread #%s for chat %s", thread_id, chat_id)


async def _wa_register_pending_thread(chat_id: str, thread_id: int) -> None:
    """Upsert a WhatsAppPendingThread row and (re)schedule idle close."""
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
        )
        db.add(WhatsAppPendingThread(chat_id=chat_id, thread_id=thread_id, expires_at=expires))
        await db.commit()
    _wa_schedule_idle_close(chat_id, thread_id)


async def _wa_close_thread(chat_id: str, farewell: str = "Talk to you later! 👋") -> None:
    """Close the pending thread and send farewell message."""
    _wa_cancel_idle_close(chat_id)
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
        )
        await db.commit()
    try:
        from app.integrations.green_api import get_green_client
        client = get_green_client()
        if client:
            await client.send_message(chat_id, farewell)
    except Exception as exc:
        logger.warning("_wa_close_thread: send failed: %s", exc)
```

- [ ] **Step 4: Add `_run_wa_interactive` helper**

After `_wa_close_thread`, add:

```python
async def _run_wa_interactive(chat_id: str, text: str, thread_id: int, sender_name: str) -> None:
    """Run the agent on an interactive-mode WhatsApp message and reply."""
    from app.web.routes.telegram import _run_direct_thread

    tagged = f"[via WhatsApp interactive] [sender: {sender_name}] {text}"
    result_text = await _run_direct_thread(tagged, thread_id)

    if result_text:
        try:
            from app.integrations.green_api import get_green_client
            client = get_green_client()
            if client:
                # WhatsApp has a 4096 char limit per message
                for chunk in _split_message(result_text, 4000):
                    await client.send_message(chat_id, chunk)
        except Exception as exc:
            logger.warning("_run_wa_interactive: send failed: %s", exc)

    await _wa_register_pending_thread(chat_id, thread_id)


def _split_message(text: str, max_len: int) -> list[str]:
    """Split long text into chunks at newline boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
```

- [ ] **Step 5: Add interactive mode routing in `_handle_incoming`**

Find the section in `_handle_incoming` that begins with `# Automation trigger` (near end of the function). Replace just the automation trigger block with this:

```python
    # Interactive mode check
    if group and group.enabled and group.interactive_mode:
        asyncio.get_running_loop().create_task(
            _handle_interactive_message(chat_id, sender_name, text, group)
        )
        return

    # Automation trigger (fire-and-forget, unchanged)
    try:
        from app.automations.runtime import on_whatsapp_message
        asyncio.get_running_loop().create_task(
            on_whatsapp_message(
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name=sender_name,
                message_text=text,
                group_name=group.name if group else "",
                message_type=_detect_message_type(message_data),
                media_url=media_url or "",
                message_id=message_id,
            )
        )
    except Exception as exc:
        logger.warning("WhatsApp automation dispatch failed: %s", exc)
```

- [ ] **Step 6: Add `_handle_interactive_message` function**

After `_handle_incoming`, add:

```python
async def _handle_interactive_message(
    chat_id: str,
    sender_name: str,
    text: str,
    group: WhatsAppGroup,
) -> None:
    """Handle a message from an interactive-mode group."""
    _wa_cancel_idle_close(chat_id)

    # Bye/exit → close thread
    if _wa_is_end_reply(text):
        await _wa_close_thread(chat_id, "Talk to you later! 👋")
        return

    # Look up existing open thread
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
        )
        pending = result.scalars().first()

    now = datetime.now(timezone.utc)

    if pending and pending.expires_at.replace(tzinfo=timezone.utc) > now:
        # Continue existing thread
        thread_id = pending.thread_id
    else:
        # Open new thread
        async with AsyncSessionLocal() as db:
            thread = Thread(
                title=f"WhatsApp: {group.name} — {text[:50]}",
                model=app_config.DEFAULT_THREAD_MODEL,
            )
            db.add(thread)
            await db.commit()
            await db.refresh(thread)
            thread_id = thread.id

    await _run_wa_interactive(chat_id, text, thread_id, sender_name)
```

- [ ] **Step 7: Verify app starts without errors**

```bash
python -c "import app.web.routes.whatsapp; print('ok')"
```
Expected: `ok`

- [ ] **Step 8: Commit**

```bash
git add app/web/routes/whatsapp.py
git commit -m "feat: WhatsApp interactive mode — persistent thread, idle close, bye/exit detection"
```

---

### Task 3: UI toggle in whatsapp.html

**Files:**
- Modify: `app/web/templates/whatsapp.html`

- [ ] **Step 1: Read current group card HTML**

Search for `auto_send_allowed` in `app/web/templates/whatsapp.html` to find where group toggles are rendered.

- [ ] **Step 2: Add interactive_mode toggle to group card**

Find the toggle for `auto_send_allowed` in the group card. It will look something like:
```html
<input type="checkbox" ... name="auto_send_allowed" ...>
```

Add a similar toggle directly below it:
```html
<label class="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
  <input type="checkbox"
         class="w-4 h-4 accent-indigo-500"
         onchange="toggleGroupField({{ group.id }}, 'interactive_mode', this.checked)"
         {% if group.interactive_mode %}checked{% endif %}>
  <span>Interactive mode</span>
  <span class="text-xs text-gray-500">(persistent thread, like Telegram)</span>
</label>
```

- [ ] **Step 3: Add API endpoint to toggle interactive_mode**

In `app/web/routes/whatsapp.py`, find the existing API endpoint for toggling group fields (search for `auto_send_allowed` in the PATCH/POST endpoints). It will be something like `/api/whatsapp/groups/{id}/toggle`. Add `interactive_mode` to the list of allowed fields in that endpoint.

Read the endpoint first to understand the exact pattern, then add `"interactive_mode"` to the allowed fields list.

- [ ] **Step 4: Verify the page loads**

Start the app (`python run.py`) and navigate to `/whatsapp`. Each group card should now show an "Interactive mode" checkbox.

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/whatsapp.html app/web/routes/whatsapp.py
git commit -m "feat: interactive mode toggle in WhatsApp group UI"
```

---

### Task 4: End-to-end test

**Files:** none — manual verification

- [ ] **Step 1: Enable interactive mode on a test group**

Go to `/whatsapp`, find a group, toggle "Interactive mode" on.

- [ ] **Step 2: Send a message from that WhatsApp group**

Send any message. Expected:
- A new thread is created in RAION
- Agent responds and sends reply back to WhatsApp group
- Idle close is scheduled (2 min)

- [ ] **Step 3: Send a follow-up in the same group within 2 minutes**

Expected: same thread continues (no new thread created), agent has context of previous exchange.

- [ ] **Step 4: Send "bye"**

Expected: RAION replies "Talk to you later! 👋" and thread is closed.

- [ ] **Step 5: Verify fire-and-forget still works**

Disable interactive mode on the group. Send a message. Expected: automation fires as before (existing behaviour unchanged).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test: WhatsApp interactive mode E2E verified"
```
