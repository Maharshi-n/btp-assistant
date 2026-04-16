# Telegram File Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept files sent via Telegram, save them to `workspace/telegram_uploads/`, and run the agent with the user's intent text + file path as one coherent prompt.

**Architecture:** A new `TelegramPendingFile` DB table stores the user's text intent when they say "I'll upload a file" without attaching one yet. The Telegram webhook is extended to detect incoming files (any type), download them via the Telegram file API, save to a fixed workspace folder, then invoke the existing `_run_direct_thread` agent path with a constructed prompt. Text-only messages are checked against a keyword heuristic to decide whether to store a pending file intent or run the agent immediately.

**Tech Stack:** Python/FastAPI, SQLAlchemy async, httpx, existing LangGraph agent pipeline (`_run_direct_thread`), Telegram Bot API (`getFile` + file download endpoints).

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `app/db/models.py` | Modify | Add `TelegramPendingFile` model |
| `app/web/routes/telegram.py` | Modify | File detection, download, pending file logic, updated webhook flow |

`app/db/engine.py` — no change needed (`init_db` calls `Base.metadata.create_all` automatically).

---

## Task 1: Add `TelegramPendingFile` model

**Files:**
- Modify: `app/db/models.py`

- [ ] **Step 1: Add the model**

Open `app/db/models.py`. After the `TelegramPendingReply` class (ends around line 155), add:

```python
class TelegramPendingFile(Base):
    __tablename__ = "telegram_pending_files"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Telegram chat ID — one row per chat (upsert pattern)
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # The user's instruction text ("save this in reports/")
    intent_text: Mapped[str] = mapped_column(Text, nullable=False)
    # DB thread to post messages into (nullable — may not have an active thread)
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True)
    # AutomationConversation id if triggered from an automation (nullable)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: Verify the app starts and creates the table**

Run:
```bash
cd "E:/BTP project" && python -c "
import asyncio
from app.db.engine import init_db
asyncio.run(init_db())
print('OK')
"
```
Expected output: `OK` (no errors, table created in `app.db`)

- [ ] **Step 3: Verify table exists in DB**

```bash
cd "E:/BTP project" && python -c "
import sqlite3
conn = sqlite3.connect('app.db')
tables = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()
print([t[0] for t in tables])
conn.close()
"
```
Expected: list includes `'telegram_pending_files'`

- [ ] **Step 4: Commit**

```bash
cd "E:/BTP project" && git add app/db/models.py && git commit -m "feat: add TelegramPendingFile model"
```

---

## Task 2: Add file download helper to telegram webhook

**Files:**
- Modify: `app/web/routes/telegram.py`

- [ ] **Step 1: Add the `_download_telegram_file` helper**

Open `app/web/routes/telegram.py`. After the imports block (after `logger = logging.getLogger(__name__)`), add this helper function:

```python
async def _download_telegram_file(token: str, message: dict) -> tuple[str, str] | None:
    """Download a file from a Telegram message to workspace/telegram_uploads/.

    Supports: document, photo (largest), audio, voice, video.
    Returns (filename, absolute_path) on success, None on failure.
    """
    import httpx
    from pathlib import Path
    import app.config as _cfg

    # Extract file_id and original filename from whichever field is present
    file_id: str | None = None
    original_name: str | None = None

    if "document" in message:
        doc = message["document"]
        file_id = doc.get("file_id")
        original_name = doc.get("file_name")
    elif "photo" in message:
        # photos is a list sorted by size — take the last (largest)
        photos = message["photo"]
        if photos:
            file_id = photos[-1].get("file_id")
            original_name = f"{file_id}.jpg"
    elif "audio" in message:
        audio = message["audio"]
        file_id = audio.get("file_id")
        original_name = audio.get("file_name") or f"{file_id}.mp3"
    elif "voice" in message:
        voice = message["voice"]
        file_id = voice.get("file_id")
        original_name = f"{file_id}.ogg"
    elif "video" in message:
        video = message["video"]
        file_id = video.get("file_id")
        original_name = video.get("file_name") or f"{file_id}.mp4"

    if not file_id:
        return None

    filename = original_name or f"{file_id}.bin"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: get the file path on Telegram's servers
            resp = await client.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id},
            )
            if resp.status_code != 200:
                logger.warning("_download_telegram_file: getFile failed %d", resp.status_code)
                return None
            tg_file_path = resp.json().get("result", {}).get("file_path")
            if not tg_file_path:
                return None

            # Step 2: download the actual bytes
            dl_resp = await client.get(
                f"https://api.telegram.org/file/bot{token}/{tg_file_path}"
            )
            if dl_resp.status_code != 200:
                logger.warning("_download_telegram_file: download failed %d", dl_resp.status_code)
                return None

            # Step 3: save to workspace/telegram_uploads/
            upload_dir = _cfg.WORKSPACE_DIR / "telegram_uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / filename
            dest.write_bytes(dl_resp.content)
            logger.info("_download_telegram_file: saved %s (%d bytes)", dest, len(dl_resp.content))
            return filename, str(dest)

    except Exception as exc:
        logger.warning("_download_telegram_file: exception: %s", exc)
        return None
```

- [ ] **Step 2: Commit**

```bash
cd "E:/BTP project" && git add app/web/routes/telegram.py && git commit -m "feat: add _download_telegram_file helper"
```

---

## Task 3: Add pending file DB helpers

**Files:**
- Modify: `app/web/routes/telegram.py`

- [ ] **Step 1: Add `_store_pending_file` and `_get_and_clear_pending_file` helpers**

After the `_download_telegram_file` function, add:

```python
# Keywords that suggest the user is about to send a file
_FILE_HINT_KEYWORDS = {
    "upload", "uploading", "sending", "will send", "attaching", "file",
    "document", "pdf", "image", "photo", "here is", "here's", "check this",
}


def _text_hints_file(text: str) -> bool:
    """Return True if the text suggests a file is about to be sent."""
    lower = text.lower()
    return any(kw in lower for kw in _FILE_HINT_KEYWORDS)


async def _store_pending_file(
    chat_id: str,
    intent_text: str,
    thread_id: int | None = None,
    conversation_id: int | None = None,
) -> None:
    """Upsert a TelegramPendingFile row for this chat_id."""
    from app.db.models import TelegramPendingFile
    from sqlalchemy import delete as sa_delete

    async with AsyncSessionLocal() as db:
        # Delete any existing pending file for this chat
        await db.execute(
            sa_delete(TelegramPendingFile).where(TelegramPendingFile.chat_id == chat_id)
        )
        db.add(TelegramPendingFile(
            chat_id=chat_id,
            intent_text=intent_text,
            thread_id=thread_id,
            conversation_id=conversation_id,
        ))
        await db.commit()


async def _get_and_clear_pending_file(chat_id: str) -> dict | None:
    """Return pending file intent dict and delete the row. Returns None if none exists."""
    from app.db.models import TelegramPendingFile
    from sqlalchemy import delete as sa_delete, select as sa_select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            sa_select(TelegramPendingFile).where(TelegramPendingFile.chat_id == chat_id)
        )
        row = result.scalars().first()
        if row is None:
            return None
        data = {
            "intent_text": row.intent_text,
            "thread_id": row.thread_id,
            "conversation_id": row.conversation_id,
        }
        await db.execute(
            sa_delete(TelegramPendingFile).where(TelegramPendingFile.chat_id == chat_id)
        )
        await db.commit()
        return data
```

- [ ] **Step 2: Commit**

```bash
cd "E:/BTP project" && git add app/web/routes/telegram.py && git commit -m "feat: add pending file helpers and file-hint heuristic"
```

---

## Task 4: Wire file handling into the webhook

**Files:**
- Modify: `app/web/routes/telegram.py`

This is the core integration task. The webhook currently has this early-exit guard:

```python
if not chat_id or not text:
    return {"ok": True}
```

We need to replace it so that messages with files (but no text) are also processed.

- [ ] **Step 1: Replace the early-exit guard and add file handling block**

Find this block in `telegram_webhook` (around line 254):

```python
    message = body.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return {"ok": True}
```

Replace it with:

```python
    message = body.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or message.get("caption") or "").strip()

    if not chat_id:
        return {"ok": True}

    # ── File handling ─────────────────────────────────────────────────────
    has_file = any(k in message for k in ("document", "photo", "audio", "voice", "video"))

    if has_file:
        result = await _download_telegram_file(token, message)
        if result is None:
            if token:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "Sorry, I couldn't download that file. Please try again."},
                        )
                except Exception:
                    pass
            return {"ok": True}

        filename, file_path = result

        # Get intent: caption > stored pending > generic fallback
        pending_file = await _get_and_clear_pending_file(chat_id)
        if text:
            intent = text
            thread_id = pending_file["thread_id"] if pending_file else None
        elif pending_file:
            intent = pending_file["intent_text"]
            thread_id = pending_file["thread_id"]
        else:
            intent = f"User sent a file via Telegram. It has been saved to: workspace/telegram_uploads/{filename}. What should I do with it?"
            thread_id = None

        # Build the full prompt for the agent
        if intent != f"User sent a file via Telegram. It has been saved to: workspace/telegram_uploads/{filename}. What should I do with it?":
            prompt = f"{intent}\n\nFile saved to: workspace/telegram_uploads/{filename}"
        else:
            prompt = intent

        # Ensure we have an active thread
        if not thread_id:
            async with AsyncSessionLocal() as db:
                from app.db.models import Thread
                new_thread = Thread(title=f"Telegram file: {filename}", model="gpt-4o")
                db.add(new_thread)
                await db.commit()
                await db.refresh(new_thread)
                thread_id = new_thread.id
            await _register_pending_reply(chat_id, thread_id, conversation_id=None)

        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": "Got it, working on it..."},
                    )
            except Exception:
                pass

        async def _run_file_task(tid: int, p: str) -> None:
            result_text = await _run_direct_thread(p, tid)
            new_pending = await _has_pending_reply(chat_id)
            if not new_pending and token:
                try:
                    reply_body = result_text[:1000] + ("..." if len(result_text) > 1000 else "")
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": reply_body},
                        )
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "Anything else?"},
                        )
                except Exception as exc:
                    logger.warning("telegram file task: failed to send result: %s", exc)
                await _register_pending_reply(chat_id, tid, conversation_id=None)

        asyncio.create_task(_run_file_task(thread_id, prompt))
        return {"ok": True}

    # ── Text-only messages ────────────────────────────────────────────────
    if not text:
        return {"ok": True}
```

- [ ] **Step 2: Add pending file check into the text-only flow**

After the existing `TelegramPendingReply` lookup block (the block that ends with `await db.commit()`), and BEFORE the `if _is_end_reply(text):` check, add the pending file handling:

Find this line:
```python
    # If user said "no"/"done"/etc., close the conversation gracefully.
    if _is_end_reply(text):
```

Just above it, insert:

```python
    # ── Clear pending file intent if user sends redirecting text ─────────
    pending_file_row = await _get_and_clear_pending_file(chat_id)
    if pending_file_row is not None:
        # User sent text while a file was expected — treat as new intent
        # Fall through to heuristic check below (don't return early)
        pass
```

Then find the very bottom of the function, after the final `return {"ok": True}` of the `if conversation_id is None:` block — specifically right before `asyncio.create_task(_run_and_notify(conversation_id))` — and add the heuristic check as a new branch.

Actually, the cleanest place is right before the existing `if pending is None: return {"ok": True}` check. Find:

```python
        if pending is None:
            return {"ok": True}
```

Replace with:

```python
        if pending is None:
            # No automation pending — check if user is hinting a file is coming
            if _text_hints_file(text):
                # Determine active thread_id if any
                active_thread_id = None
                async with AsyncSessionLocal() as db2:
                    from sqlalchemy import select as _sel
                    from app.db.models import TelegramPendingReply as _TPR
                    _now = datetime.now(timezone.utc)
                    # Re-query since we just cleared — but there shouldn't be one
                    # Use the last known thread if available from pending_file_row
                    pass
                if pending_file_row:
                    active_thread_id = pending_file_row.get("thread_id")
                await _store_pending_file(chat_id, text, thread_id=active_thread_id)
                if token:
                    try:
                        async with httpx.AsyncClient(timeout=5) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": "Got it, send the file when ready."},
                            )
                    except Exception:
                        pass
                return {"ok": True}
            return {"ok": True}
```

- [ ] **Step 3: Add `import httpx` at top of file if not already present**

Check the top of `app/web/routes/telegram.py` — it currently imports httpx inside functions. Add at the top-level imports:

```python
import httpx
```

- [ ] **Step 4: Verify the server starts without errors**

```bash
cd "E:/BTP project" && python -c "
import asyncio
from app.main import app
print('Import OK')
"
```
Expected: `Import OK`

- [ ] **Step 5: Commit**

```bash
cd "E:/BTP project" && git add app/web/routes/telegram.py && git commit -m "feat: handle Telegram file uploads in webhook"
```

---

## Task 5: End-to-end smoke test

**Files:**
- No file changes — manual verification steps

- [ ] **Step 1: Start the server**

```bash
cd "E:/BTP project" && python run.py
```

- [ ] **Step 2: Send a text hint via Telegram**

Send to your bot: `"I'll upload a file, save it in the reports folder"`

Expected bot reply: `"Got it, send the file when ready."`

- [ ] **Step 3: Verify pending row was created**

```bash
cd "E:/BTP project" && python -c "
import sqlite3
conn = sqlite3.connect('app.db')
rows = conn.execute('SELECT chat_id, intent_text FROM telegram_pending_files').fetchall()
print(rows)
conn.close()
"
```
Expected: one row with your chat_id and the intent text.

- [ ] **Step 4: Send a document file via Telegram**

Send any file (PDF, txt, image) to the bot.

Expected:
- Bot replies: `"Got it, working on it..."`
- File appears at `workspace/telegram_uploads/<filename>`
- Agent runs and bot replies with result
- Bot asks: `"Anything else?"`

- [ ] **Step 5: Verify file was saved**

```bash
ls "E:/BTP project/workspace/telegram_uploads/"
```
Expected: the filename you sent.

- [ ] **Step 6: Commit**

```bash
cd "E:/BTP project" && git commit --allow-empty -m "feat: telegram file upload complete"
```
