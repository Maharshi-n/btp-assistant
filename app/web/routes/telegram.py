"""Telegram webhook — receives incoming messages and resumes pending automations."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import delete, select

import app.config as app_config
from app.db.engine import AsyncSessionLocal
import httpx

from app.db.models import Message, TelegramPendingFile, TelegramPendingFileItem, TelegramPendingReply, Thread

logger = logging.getLogger(__name__)


async def _download_telegram_file(token: str, message: dict) -> tuple[str, str] | None:
    """Download a file from a Telegram message to workspace/telegram_uploads/.

    Supports: document, photo (largest), audio, voice, video.
    Returns (filename, absolute_path) on success, None on failure.
    """
    file_id: str | None = None
    original_name: str | None = None

    if "document" in message:
        doc = message["document"]
        file_id = doc.get("file_id")
        original_name = doc.get("file_name")
    elif "photo" in message:
        photos = message["photo"]
        if photos:
            file_id = photos[-1].get("file_id")
            # Photos have no original filename — use a short timestamp
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            original_name = f"photo_{ts}.jpg"
    elif "audio" in message:
        audio = message["audio"]
        file_id = audio.get("file_id")
        original_name = audio.get("file_name") or f"audio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
    elif "voice" in message:
        voice = message["voice"]
        file_id = voice.get("file_id")
        original_name = f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg"
    elif "video" in message:
        video = message["video"]
        file_id = video.get("file_id")
        original_name = video.get("file_name") or f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"

    if not file_id:
        return None

    filename = (original_name or "").strip() or f"file_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bin"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
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

            dl_resp = await client.get(
                f"https://api.telegram.org/file/bot{token}/{tg_file_path}"
            )
            if dl_resp.status_code != 200:
                logger.warning("_download_telegram_file: download failed %d", dl_resp.status_code)
                return None

            upload_dir = app_config.WORKSPACE_DIR / "telegram_uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / filename
            dest.write_bytes(dl_resp.content)
            logger.info("_download_telegram_file: saved %s (%d bytes)", dest, len(dl_resp.content))
            return filename, str(dest)

    except Exception as exc:
        logger.warning("_download_telegram_file: exception: %s", exc)
        return None


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
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(TelegramPendingFile).where(TelegramPendingFile.chat_id == chat_id)
        )
        db.add(TelegramPendingFile(
            chat_id=chat_id,
            intent_text=intent_text,
            thread_id=thread_id,
            conversation_id=conversation_id,
            expires_at=expires,
        ))
        await db.commit()


async def _get_and_clear_pending_file(chat_id: str) -> dict | None:
    """Return pending file intent dict and delete the row. Returns None if none exists."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramPendingFile)
            .where(TelegramPendingFile.chat_id == chat_id)
            .where(TelegramPendingFile.expires_at > datetime.now(timezone.utc))
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
            delete(TelegramPendingFile).where(TelegramPendingFile.chat_id == chat_id)
        )
        await db.commit()
        return data


async def _add_pending_file_item(chat_id: str, filename: str, file_path: str) -> int:
    """Append one downloaded file to the multi-file accumulation queue.

    Returns the total number of accumulated files for this chat_id (after adding).
    """
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    async with AsyncSessionLocal() as db:
        db.add(TelegramPendingFileItem(
            chat_id=chat_id,
            filename=filename,
            file_path=file_path,
            expires_at=expires,
        ))
        await db.commit()
        result = await db.execute(
            select(TelegramPendingFileItem)
            .where(TelegramPendingFileItem.chat_id == chat_id)
            .where(TelegramPendingFileItem.expires_at > datetime.now(timezone.utc))
        )
        return len(result.scalars().all())


async def _get_and_clear_pending_file_items(chat_id: str) -> list[dict]:
    """Return all accumulated file items for this chat_id and delete them.

    Returns a list of dicts with 'filename' and 'file_path' keys.
    Returns an empty list if none exist.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramPendingFileItem)
            .where(TelegramPendingFileItem.chat_id == chat_id)
            .where(TelegramPendingFileItem.expires_at > now)
            .order_by(TelegramPendingFileItem.created_at.asc())
        )
        rows = result.scalars().all()
        if not rows:
            return []
        items = [{"filename": r.filename, "file_path": r.file_path} for r in rows]
        await db.execute(
            delete(TelegramPendingFileItem).where(TelegramPendingFileItem.chat_id == chat_id)
        )
        await db.commit()
        return items


async def _transcribe_voice(file_path: str) -> str | None:
    """Transcribe a voice note using OpenAI Whisper. Returns transcript or None on failure."""
    from openai import AsyncOpenAI
    import app.config as _cfg
    try:
        client = AsyncOpenAI(api_key=_cfg.OPENAI_API_KEY)
        with open(file_path, "rb") as f:
            resp = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        transcript = resp.strip() if isinstance(resp, str) else str(resp).strip()
        logger.info("_transcribe_voice: transcribed %s → %d chars", file_path, len(transcript))
        return transcript or None
    except Exception as exc:
        logger.warning("_transcribe_voice: failed: %s", exc)
        return None


async def _parse_reminder_datetime(text: str) -> datetime | None:
    """Use OpenAI to extract a future datetime from natural language reminder text.

    Returns a timezone-aware datetime or None if parsing fails.
    """
    from openai import AsyncOpenAI
    import app.config as _cfg
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = (
        f"Current time: {now_str}\n"
        f"User said: \"{text}\"\n\n"
        "Extract the datetime the user wants to be reminded. "
        "Output ONLY an ISO 8601 datetime string in UTC (e.g. 2026-04-16T17:00:00Z). "
        "If you cannot determine a specific future time, output: UNKNOWN"
    )
    try:
        client = AsyncOpenAI(api_key=_cfg.OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=30,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw == "UNKNOWN":
            return None
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


async def _parse_and_schedule_reminder(remind_text: str, chat_id: str, token: str | None) -> str:
    """Parse reminder text, schedule a one-shot APScheduler job, return confirmation string."""
    fire_at = await _parse_reminder_datetime(remind_text)
    if fire_at is None:
        return "Couldn't understand the time. Try: /remind at 5pm check the report"

    now = datetime.now(timezone.utc)
    if fire_at <= now:
        return "That time is in the past. Please specify a future time."

    # The reminder message is everything after the time expression.
    # We pass the full remind_text as the reminder content — the agent sent it.
    reminder_msg = remind_text

    from app.automations.runtime import get_scheduler
    from apscheduler.triggers.date import DateTrigger

    scheduler = get_scheduler()
    if scheduler is None or not scheduler.running:
        return "Scheduler is not running. Please restart RAION."

    job_id = f"remind_{chat_id}_{int(fire_at.timestamp())}"
    scheduler.add_job(
        _fire_reminder,
        DateTrigger(run_date=fire_at),
        id=job_id,
        args=[chat_id, token, reminder_msg],
        replace_existing=True,
        max_instances=1,
    )

    # Human-friendly confirmation
    local_str = fire_at.strftime("%b %d at %H:%M UTC")
    return f"Reminder set for {local_str}."


async def _fire_reminder(chat_id: str, token: str | None, message: str) -> None:
    """Fire a one-shot reminder by running the agent with the reminder text as the prompt.

    Creates a new thread, runs the supervisor, sends the result to Telegram.
    Falls back to a plain text reminder if the agent run fails.
    """
    if not token:
        return
    try:
        async with AsyncSessionLocal() as db:
            thread = Thread(title=f"Reminder: {message[:50]}", model="gpt-4o-mini")
            db.add(thread)
            await db.commit()
            await db.refresh(thread)
            thread_id = thread.id

        tagged = f"[via Telegram] [Reminder triggered] {message}"
        result_text = await _run_direct_thread(tagged, thread_id)
        reply = _smart_truncate(result_text) if result_text and result_text != "Done." else f"Reminder: {message}"
    except Exception as exc:
        logger.warning("_fire_reminder: agent run failed: %s", exc)
        reply = f"Reminder: {message}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": reply},
            )
            if reply != f"Reminder: {message}":
                # Re-register pending so the user can follow up
                await _register_pending_reply(chat_id, thread_id, conversation_id=None)
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": "..."},
                )
    except Exception as exc:
        logger.warning("_fire_reminder: failed to send result: %s", exc)


def _smart_truncate(text: str, limit: int = 3800) -> str:
    """Truncate text at a sentence/line boundary near `limit` chars.

    Stays well under Telegram's 4096-char message limit.
    If the text is short enough, returns it unchanged.
    """
    if len(text) <= limit:
        return text
    # Try to cut at last newline before limit
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        # No good newline — cut at last sentence boundary
        for sep in (". ", "! ", "? "):
            pos = text.rfind(sep, 0, limit)
            if pos > limit // 2:
                cut = pos + 1
                break
        else:
            cut = limit
    return text[:cut].rstrip() + "\n\n_(truncated — see full reply in RAION)_"


router = APIRouter()

# Phrases that mean the user wants to end the conversation
_END_PHRASES = {
    "no", "nope", "nah", "nothing", "nothing else", "that's all", "thats all",
    "that's it", "thats it", "done", "bye", "goodbye", "thanks", "thank you",
    "ok thanks", "ok thank you", "no thanks", "no thank you", "all good",
    "i'm good", "im good", "stop", "exit", "quit", "end",
}


def _is_end_reply(text: str) -> bool:
    """Return True if the user's reply signals end-of-conversation."""
    return text.lower().strip().rstrip("!.") in _END_PHRASES


async def _register_pending_reply(
    chat_id: str,
    thread_id: int,
    conversation_id: int | None = None,
) -> None:
    """Re-register a TelegramPendingReply so the next user message continues the loop."""
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
        )
        pending = TelegramPendingReply(
            chat_id=chat_id,
            continuation_prompt="",
            last_question="Anything else?",
            thread_id=thread_id,
            conversation_id=conversation_id,
            expires_at=expires,
        )
        db.add(pending)
        await db.commit()


async def _has_pending_reply(chat_id: str) -> bool:
    """Return True if a non-expired pending reply exists for this chat_id."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramPendingReply)
            .where(TelegramPendingReply.chat_id == chat_id)
            .where(TelegramPendingReply.expires_at > now)
        )
        return result.scalars().first() is not None


async def _run_direct_thread(user_reply: str, db_thread_id: int, file_context: str | None = None) -> str:
    """Resume a direct chat LangGraph thread (no AutomationConversation) with the user's reply.

    Used when telegram_ask was called from a plain chat thread rather than an automation.
    The LangGraph thread key is str(db_thread_id) — same as the chat route uses.

    file_context: if set, appended as a silent system note (not shown to user) so the agent
    knows the file path without polluting the visible conversation.
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from app.agents.supervisor import get_graph

    lg_thread_id = str(db_thread_id)

    # Tag the message as coming from Telegram — silent context for the agent
    tagged_reply = f"[via Telegram] {user_reply}"

    # Persist user reply into the DB thread so it shows in the UI (without the tag)
    async with AsyncSessionLocal() as db:
        thread = await db.get(Thread, db_thread_id)
        model = thread.model if thread else "gpt-4o-mini"
        msg = Message(
            thread_id=db_thread_id,
            role="user",
            content=user_reply,
            metadata_json=json.dumps({"telegram_reply": True}),
        )
        db.add(msg)
        await db.commit()

    graph = get_graph()
    lg_config = {
        "configurable": {
            "thread_id": lg_thread_id,
            "ws_thread_id": db_thread_id,
            "model": model,
            "automation_run": True,
        }
    }

    lc_messages: list = [HumanMessage(content=tagged_reply)]
    # Inject file path as a system note so it doesn't appear in the user message
    if file_context:
        lc_messages.insert(0, SystemMessage(content=file_context))
    full_content: list[str] = []
    last_ai_content: str = ""

    try:
        async for event in graph.astream_events(
            {"messages": lc_messages}, lg_config, version="v2"
        ):
            event_type = event.get("event", "")
            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and chunk.content:
                    full_content.append(chunk.content)
            elif event_type == "on_chain_end" and event.get("name") == "supervisor":
                output = event.get("data", {}).get("output", {})
                msgs = output.get("messages", []) if isinstance(output, dict) else []
                for m in reversed(msgs):
                    if isinstance(m, AIMessage) and m.content:
                        last_ai_content = m.content if isinstance(m.content, str) else str(m.content)
                        break

        final_content = "".join(full_content) or last_ai_content

        if final_content:
            async with AsyncSessionLocal() as db2:
                msg = Message(
                    thread_id=db_thread_id,
                    role="assistant",
                    content=final_content,
                    metadata_json=json.dumps({"telegram_reply": True}),
                )
                db2.add(msg)
                await db2.commit()

        return final_content or "Done."

    except Exception as exc:
        logger.exception("telegram webhook: direct thread supervisor run failed: %s", exc)
        return f"Error during execution: {exc}"


async def _run_continuation(user_reply: str, conversation_id: int) -> str:
    """Resume the automation's LangGraph thread with the user's Telegram reply.

    Loads the existing lg_thread_id from AutomationConversation so the LLM
    sees the full prior conversation — no prompt injection, no placeholders,
    no context re-assembly. The LLM naturally continues where it left off.
    """
    from app.automations.conversations import get_conversation
    from langchain_core.messages import AIMessage, HumanMessage
    from app.agents.supervisor import get_graph

    conv = await get_conversation(conversation_id)
    if conv is None:
        logger.warning("_run_continuation: conversation %d not found", conversation_id)
        return "Error: conversation not found."

    lg_thread_id = conv.lg_thread_id
    db_thread_id = conv.db_thread_id

    if not lg_thread_id:
        logger.warning("_run_continuation: conversation %d has no lg_thread_id", conversation_id)
        return "Error: conversation thread not found."

    # Persist user reply into the same DB thread so it appears in the UI
    if db_thread_id:
        async with AsyncSessionLocal() as db:
            msg = Message(
                thread_id=db_thread_id,
                role="user",
                content=user_reply,
                metadata_json=json.dumps({"telegram_reply": True}),
            )
            db.add(msg)
            await db.commit()

    graph = get_graph()
    lg_config = {
        "configurable": {
            "thread_id": lg_thread_id,       # same thread — LLM sees full history
            "ws_thread_id": db_thread_id or 0,
            "model": "gpt-4o-mini",
            "automation_run": True,
        }
    }

    # Send just the user's reply — the LangGraph checkpointer carries all prior context
    lc_messages = [HumanMessage(content=user_reply)]
    full_content: list[str] = []
    last_ai_content: str = ""

    try:
        async for event in graph.astream_events(
            {"messages": lc_messages}, lg_config, version="v2"
        ):
            event_type = event.get("event", "")
            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and chunk.content:
                    full_content.append(chunk.content)
            elif event_type == "on_chain_end" and event.get("name") == "supervisor":
                output = event.get("data", {}).get("output", {})
                msgs = output.get("messages", []) if isinstance(output, dict) else []
                for m in reversed(msgs):
                    if isinstance(m, AIMessage) and m.content:
                        last_ai_content = m.content if isinstance(m.content, str) else str(m.content)
                        break

        final_content = "".join(full_content) or last_ai_content

        # Persist assistant reply into the same DB thread
        if final_content and db_thread_id:
            async with AsyncSessionLocal() as db2:
                msg = Message(
                    thread_id=db_thread_id,
                    role="assistant",
                    content=final_content,
                    metadata_json=json.dumps({"telegram_reply": True}),
                )
                db2.add(msg)
                await db2.commit()

        return final_content or "Done."

    except Exception as exc:
        logger.exception("telegram webhook: supervisor run failed: %s", exc)
        return f"Error during execution: {exc}"


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Receive Telegram webhook updates and resume pending automations."""
    expected_secret = app_config.TELEGRAM_WEBHOOK_SECRET
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    message = body.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or message.get("caption") or "").strip()

    if not chat_id:
        return {"ok": True}

    token = app_config.TELEGRAM_BOT_TOKEN

    # ── Voice message → Whisper transcription ─────────────────────────────
    # Voice notes are treated as spoken text prompts, not file uploads.
    if "voice" in message:
        voice_result = await _download_telegram_file(token, message)
        if voice_result is None:
            if token:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "Sorry, I couldn't download the voice message."},
                        )
                except Exception:
                    pass
            return {"ok": True}

        _, voice_path = voice_result
        transcript = await _transcribe_voice(voice_path)
        if transcript is None:
            if token:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "Sorry, I couldn't transcribe the voice message."},
                        )
                except Exception:
                    pass
            return {"ok": True}

        # Echo transcript back so user knows what was heard
        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": f"🎙 {transcript}"},
                    )
            except Exception:
                pass

        # Treat transcript as the user's text message — fall through to normal flow
        text = transcript

    # ── File handling ─────────────────────────────────────────────────────
    has_file = any(k in message for k in ("document", "photo", "audio", "video"))

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

        # Determine intent: caption > stored pending intent > None (accumulate)
        pending_file = await _get_and_clear_pending_file(chat_id)
        if text:
            if pending_file:
                logger.info(
                    "telegram file upload: caption overrides stored pending intent for chat_id=%s",
                    chat_id,
                )
            intent = text
        elif pending_file:
            intent = pending_file["intent_text"]
        else:
            intent = None

        if intent is None:
            # No intent yet — accumulate this file and ask what to do with it.
            # Any previously accumulated files stay queued too.
            total = await _add_pending_file_item(chat_id, filename, file_path)
            if token:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        if total == 1:
                            prompt_txt = f"Got {filename}. What should I do with it? (Send more files or tell me now)"
                        else:
                            prompt_txt = f"Got {filename} ({total} files queued). Send more or tell me what to do, or type 'done' to process with a generic action."
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": prompt_txt},
                        )
                except Exception:
                    pass
            return {"ok": True}

        # We have an intent — also grab any previously accumulated files and process all together.
        accumulated = await _get_and_clear_pending_file_items(chat_id)
        # Add the current file to the batch
        all_files = accumulated + [{"filename": filename, "file_path": file_path}]

        if len(all_files) == 1:
            file_context = (
                f"[File context] The user sent a file via Telegram. "
                f"It has been saved to workspace/telegram_uploads/{filename}. "
                f"Use filesystem tools (copy_file, move_file, list_dir, etc.) to act on it. "
                f"Do NOT upload to Google Drive or read binary content unless explicitly asked."
            )
        else:
            file_list = "\n".join(
                f"  - workspace/telegram_uploads/{f['filename']}" for f in all_files
            )
            file_context = (
                f"[File context] The user sent {len(all_files)} files via Telegram:\n{file_list}\n"
                f"Use filesystem tools (copy_file, move_file, list_dir, etc.) to act on them. "
                f"Do NOT upload to Google Drive or read binary content unless explicitly asked."
            )

        prompt = intent

        # Reuse active thread from TelegramPendingReply (and clear it so _has_pending_reply
        # returns False after the agent runs), then pending file's thread, then create new.
        thread_id: int | None = None
        now_file = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            tpr_result = await db.execute(
                select(TelegramPendingReply)
                .where(TelegramPendingReply.chat_id == chat_id)
                .where(TelegramPendingReply.expires_at > now_file)
            )
            active_tpr = tpr_result.scalars().first()
            if active_tpr:
                thread_id = active_tpr.thread_id
                # Clear it now so _has_pending_reply is False when _run_file_task checks
                await db.execute(
                    delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
                )
                await db.commit()

        if not thread_id and pending_file:
            thread_id = pending_file.get("thread_id")

        if not thread_id:
            async with AsyncSessionLocal() as db:
                thread_title = (intent or filename)[:60]
                new_thread = Thread(title=thread_title, model="gpt-4o-mini")
                db.add(new_thread)
                await db.commit()
                await db.refresh(new_thread)
                thread_id = new_thread.id

        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": "Got it, working on it..."},
                    )
            except Exception:
                pass

        async def _run_file_task(tid: int, p: str, fc: str) -> None:
            result_text = await _run_direct_thread(p, tid, file_context=fc)
            new_pending = await _has_pending_reply(chat_id)
            if not new_pending and token:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": _smart_truncate(result_text)},
                        )
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "..."},
                        )
                except Exception as exc:
                    logger.warning("telegram file task: failed to send result: %s", exc)
            await _register_pending_reply(chat_id, tid, conversation_id=None)

        asyncio.create_task(_run_file_task(thread_id, prompt, file_context))
        return {"ok": True}

    # ── Text-only messages ────────────────────────────────────────────────
    if not text:
        return {"ok": True}

    # Normalise slash commands: strip spaces after the slash so
    # "/ remember foo" is treated the same as "/remember foo"
    _text_norm = ("/" + text.lstrip("/").strip()) if text.startswith("/") else text

    # ── /newthread [optional title] ────────────────────────────────────────
    if _text_norm.lower().startswith("/newthread"):
        title = _text_norm[len("/newthread"):].strip() or "New Chat"
        async with AsyncSessionLocal() as db:
            thread = Thread(title=title, model="gpt-4o-mini")
            db.add(thread)
            await db.commit()
            await db.refresh(thread)
            new_thread_id = thread.id

        # Clear any existing pending reply and point to the new thread
        await _register_pending_reply(chat_id, new_thread_id, conversation_id=None)

        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": "Hey! How can I help you?",
                        },
                    )
            except Exception:
                pass
        return {"ok": True}

    # ── /thread — show active thread ──────────────────────────────────────
    if _text_norm.lower() == "/thread":
        now_check = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TelegramPendingReply)
                .where(TelegramPendingReply.chat_id == chat_id)
                .where(TelegramPendingReply.expires_at > now_check)
            )
            active = result.scalars().first()

        if active:
            async with AsyncSessionLocal() as db:
                thread = await db.get(Thread, active.thread_id)
            if thread:
                reply = f"Active thread: \"{thread.title}\" (#{thread.id})"
            else:
                reply = f"Active thread ID: #{active.thread_id}"
        else:
            reply = "No active thread. Send /newthread to start one."

        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": reply},
                    )
            except Exception:
                pass
        return {"ok": True}

    # ── /model [model_name] — change model for active thread ─────────────────
    if _text_norm.lower().startswith("/model"):
        from app.web.routes.chat import AVAILABLE_MODELS as _VALID_MODELS_LIST
        _VALID_MODELS = set(_VALID_MODELS_LIST)
        requested = _text_norm[len("/model"):].strip().lower()

        if not requested:
            # Show current model
            now_check2 = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(TelegramPendingReply)
                    .where(TelegramPendingReply.chat_id == chat_id)
                    .where(TelegramPendingReply.expires_at > now_check2)
                )
                active = result.scalars().first()
            if active:
                async with AsyncSessionLocal() as db:
                    thread = await db.get(Thread, active.thread_id)
                reply = f"Current model: {thread.model if thread else 'unknown'}\nAvailable: {', '.join(sorted(_VALID_MODELS))}"
            else:
                reply = "No active thread. Send /newthread first."
        elif requested not in _VALID_MODELS:
            reply = f"Unknown model '{requested}'.\nAvailable: {', '.join(sorted(_VALID_MODELS))}"
        else:
            # Update the active thread's model
            now_check2 = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(TelegramPendingReply)
                    .where(TelegramPendingReply.chat_id == chat_id)
                    .where(TelegramPendingReply.expires_at > now_check2)
                )
                active = result.scalars().first()
            if not active:
                reply = "No active thread. Send /newthread first."
            else:
                async with AsyncSessionLocal() as db:
                    thread = await db.get(Thread, active.thread_id)
                    if thread:
                        thread.model = requested
                        await db.commit()
                        reply = f"Model changed to {requested} for this thread."
                    else:
                        reply = "Active thread not found."

        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": reply},
                    )
            except Exception:
                pass
        return {"ok": True}

    # ── /help — list all commands ─────────────────────────────────────────────
    if _text_norm.lower() in ("/help", "/help "):
        reply = (
            "Commands:\n"
            "/newthread [title] — start a new thread\n"
            "/thread — show active thread\n"
            "/model [name] — view or change AI model\n"
            "/remember <fact> — save something to memory\n"
            "/ls [folder] — list workspace files\n"
            "/remind <when> <what> — set a one-off reminder\n"
            "  e.g. /remind at 5pm review the report\n"
            "  e.g. /remind in 30 minutes check email\n\n"
            "Send a file anytime — I'll ask what to do with it.\n"
            "Send multiple files then type 'done' to process them all together."
        )
        # Append enabled custom commands
        async with AsyncSessionLocal() as db:
            from app.db.models import TelegramCommand as _TGCmdHelp
            _help_result = await db.execute(
                select(_TGCmdHelp)
                .where(_TGCmdHelp.enabled == True)  # noqa: E712
                .order_by(_TGCmdHelp.name)
            )
            _custom_cmds = _help_result.scalars().all()
        if _custom_cmds:
            reply += "\n\nCustom commands:\n"
            reply += "\n".join(f"/{c.name} — {c.description}" for c in _custom_cmds)
        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": reply},
                    )
            except Exception:
                pass
        return {"ok": True}

    # ── /remember <text> — save a memory entry ───────────────────────────────
    if _text_norm.lower().startswith("/remember") or _text_norm.lower().startswith("/memory "):
        _cmd = "/remember" if _text_norm.lower().startswith("/remember") else "/memory"
        fact = _text_norm[len(_cmd):].strip()
        if not fact:
            reply = "Usage: /remember <fact>\nExample: /remember My birthday is Jan 1"
        else:
            async with AsyncSessionLocal() as db:
                from app.db.models import UserMemory
                entry = UserMemory(content=fact)
                db.add(entry)
                await db.commit()
            from app.agents.supervisor import invalidate_memory_cache
            invalidate_memory_cache()
            reply = f"Got it, remembered:\n{fact}"
        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": reply},
                    )
            except Exception:
                pass
        return {"ok": True}

    # ── /ls [folder] — list workspace files ──────────────────────────────────
    if _text_norm.lower().startswith("/ls"):
        import app.config as _cfg
        folder = _text_norm[len("/ls"):].strip() or "."
        workspace = _cfg.WORKSPACE_DIR
        target = (workspace / folder).resolve()
        # Safety: must stay inside workspace
        try:
            target.relative_to(workspace)
        except ValueError:
            reply = "Access denied: path is outside the workspace."
            target = None
        if target is not None:
            if not target.exists():
                reply = f"Folder not found: {folder}"
            elif not target.is_dir():
                reply = f"'{folder}' is a file, not a folder."
            else:
                entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
                if not entries:
                    reply = f"{folder or 'workspace'}/  (empty)"
                else:
                    lines = []
                    for e in entries[:40]:  # cap at 40 to avoid Telegram message limits
                        prefix = "📄" if e.is_file() else "📁"
                        size = f"  {e.stat().st_size:,}B" if e.is_file() else ""
                        lines.append(f"{prefix} {e.name}{size}")
                    if len(entries) > 40:
                        lines.append(f"… and {len(entries) - 40} more")
                    header = f"📂 {folder or 'workspace'}/"
                    reply = header + "\n" + "\n".join(lines)
        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": reply},
                    )
            except Exception:
                pass
        return {"ok": True}

    # ── /remind <when> <what> — one-off reminder via Telegram ────────────────
    if _text_norm.lower().startswith("/remind"):
        remind_text = _text_norm[len("/remind"):].strip()
        if not remind_text:
            reply = "Usage: /remind <when> <what>\nExamples:\n  /remind at 5pm review the report\n  /remind in 30 minutes check email"
        else:
            reply = await _parse_and_schedule_reminder(remind_text, chat_id, token)
        if token:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": reply},
                    )
            except Exception:
                pass
        return {"ok": True}

    # ── Custom Telegram commands ──────────────────────────────────────────────
    if _text_norm.startswith("/"):
        _cmd_slug = _text_norm[1:].split()[0].lower()  # e.g. "standup" from "/standup focus"
        async with AsyncSessionLocal() as db:
            from app.db.models import TelegramCommand as _TGCmd
            _cmd_result = await db.execute(
                select(_TGCmd)
                .where(_TGCmd.name == _cmd_slug)
                .where(_TGCmd.enabled == True)  # noqa: E712
            )
            _custom_cmd = _cmd_result.scalars().first()

        if _custom_cmd is not None:
            # Block if a conversation is already in progress
            if await _has_pending_reply(chat_id):
                if token:
                    try:
                        async with httpx.AsyncClient(timeout=5) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": "Finish your current conversation first."},
                            )
                    except Exception:
                        pass
                return {"ok": True}

            # Build prompt: preset + user extra
            _user_extra = _text_norm[1 + len(_cmd_slug):].strip()
            if _custom_cmd.preset_prompt and _user_extra:
                _cmd_prompt = f"{_custom_cmd.preset_prompt}. User added: {_user_extra}"
            elif _custom_cmd.preset_prompt:
                _cmd_prompt = _custom_cmd.preset_prompt
            elif _user_extra:
                _cmd_prompt = _user_extra
            else:
                if token:
                    try:
                        async with httpx.AsyncClient(timeout=5) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": "Please provide a prompt or set a default one in the web UI."},
                            )
                    except Exception:
                        pass
                return {"ok": True}

            # Create a fresh thread for this command
            async with AsyncSessionLocal() as db:
                _cmd_thread = Thread(
                    title=f"/{_custom_cmd.name}: {_cmd_prompt[:50]}",
                    model="gpt-4o-mini",
                )
                db.add(_cmd_thread)
                await db.commit()
                await db.refresh(_cmd_thread)
                _cmd_thread_id = _cmd_thread.id

            if token:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "Got it, working on it..."},
                        )
                except Exception:
                    pass

            async def _run_custom_cmd(tid: int, prompt: str) -> None:
                result_text = await _run_direct_thread(prompt, tid)
                new_pending = await _has_pending_reply(chat_id)
                if not new_pending and token:
                    try:
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": _smart_truncate(result_text)},
                            )
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": "..."},
                            )
                    except Exception as exc:
                        logger.warning("telegram custom cmd: failed to send result: %s", exc)
                await _register_pending_reply(chat_id, tid, conversation_id=None)

            asyncio.create_task(_run_custom_cmd(_cmd_thread_id, _cmd_prompt))
            return {"ok": True}

    # ── Custom user-defined commands ──────────────────────────────────────────
    if _text_norm.startswith("/"):
        _cmd_name = _text_norm[1:].split()[0].lower()  # e.g. "mnsummary" from "/mnsummary foo"
        _cmd_args = _text_norm[len("/" + _cmd_name):].strip()  # everything after the command name
        async with AsyncSessionLocal() as db:
            from app.db.models import TelegramCommand as _TGCmd
            _cmd_result = await db.execute(
                select(_TGCmd)
                .where(_TGCmd.name == _cmd_name)
                .where(_TGCmd.enabled == True)  # noqa: E712
            )
            _custom_cmd = _cmd_result.scalars().first()
        if _custom_cmd is not None:
            # Expand preset_prompt; append any args the user typed after the command
            _base_prompt = _custom_cmd.preset_prompt or _custom_cmd.description
            _full_prompt = f"{_base_prompt}\n\n{_cmd_args}".strip() if _cmd_args else _base_prompt

            # Resolve or create thread
            _cmd_thread_id: int | None = None
            _now_cmd = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                _tpr_r = await db.execute(
                    select(TelegramPendingReply)
                    .where(TelegramPendingReply.chat_id == chat_id)
                    .where(TelegramPendingReply.expires_at > _now_cmd)
                )
                _active_tpr = _tpr_r.scalars().first()
                if _active_tpr:
                    _cmd_thread_id = _active_tpr.thread_id
                    await db.execute(
                        delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
                    )
                    await db.commit()
            if not _cmd_thread_id:
                async with AsyncSessionLocal() as db:
                    _ct = Thread(title=f"/{_cmd_name}", model="gpt-4o-mini")
                    db.add(_ct)
                    await db.commit()
                    await db.refresh(_ct)
                    _cmd_thread_id = _ct.id

            if token:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "Got it, working on it..."},
                        )
                except Exception:
                    pass

            async def _run_custom_cmd(tid: int, prompt: str) -> None:
                result_text = await _run_direct_thread(prompt, tid)
                new_pending = await _has_pending_reply(chat_id)
                if not new_pending and token:
                    try:
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": _smart_truncate(result_text)},
                            )
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": "..."},
                            )
                    except Exception as exc:
                        logger.warning("telegram custom cmd: failed to send result: %s", exc)
                    await _register_pending_reply(chat_id, tid, conversation_id=None)

            asyncio.create_task(_run_custom_cmd(_cmd_thread_id, _full_prompt))
            return {"ok": True}

    # Check end-of-conversation BEFORE looking up pending — if the user says
    # "no"/"done"/etc., we close the loop without running the supervisor.

    # Look up non-expired pending reply for this chat_id
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramPendingReply)
            .where(TelegramPendingReply.chat_id == chat_id)
            .where(TelegramPendingReply.expires_at > now)
        )
        pending = result.scalars().first()

        if pending is None:
            # ── Multi-file "done" or intent for accumulated files ─────────────────
            # If the user has queued files and now sends text, treat it as the intent.
            # "done" (or similar) triggers processing with a generic instruction.
            _accumulated = await _get_and_clear_pending_file_items(chat_id)
            if _accumulated:
                _done_words = {"done", "process", "go", "ok", "okay", "proceed", "yes"}
                _lower_text = text.lower().strip().rstrip("!.?")
                _is_done_signal = _lower_text in _done_words
                _batch_intent = (
                    "Process these files — copy or organise them as appropriate."
                    if _is_done_signal
                    else text
                )
                _file_list = "\n".join(
                    f"  - workspace/telegram_uploads/{f['filename']}" for f in _accumulated
                )
                _batch_file_context = (
                    f"[File context] The user sent {len(_accumulated)} file(s) via Telegram:\n{_file_list}\n"
                    f"Use filesystem tools (copy_file, move_file, list_dir, etc.) to act on them. "
                    f"Do NOT upload to Google Drive or read binary content unless explicitly asked."
                )
                # Resolve thread
                _batch_thread_id: int | None = None
                _now_batch = datetime.now(timezone.utc)
                async with AsyncSessionLocal() as db:
                    _tpr_r = await db.execute(
                        select(TelegramPendingReply)
                        .where(TelegramPendingReply.chat_id == chat_id)
                        .where(TelegramPendingReply.expires_at > _now_batch)
                    )
                    _active_tpr = _tpr_r.scalars().first()
                    if _active_tpr:
                        _batch_thread_id = _active_tpr.thread_id
                        await db.execute(
                            delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
                        )
                        await db.commit()
                if not _batch_thread_id:
                    async with AsyncSessionLocal() as db:
                        _bt = Thread(title=_batch_intent[:60], model="gpt-4o-mini")
                        db.add(_bt)
                        await db.commit()
                        await db.refresh(_bt)
                        _batch_thread_id = _bt.id
                if token:
                    try:
                        async with httpx.AsyncClient(timeout=5) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": "Got it, working on it..."},
                            )
                    except Exception:
                        pass

                async def _run_batch(tid: int, intent_p: str, fc: str) -> None:
                    result_text = await _run_direct_thread(intent_p, tid, file_context=fc)
                    new_pending = await _has_pending_reply(chat_id)
                    if not new_pending and token:
                        try:
                            async with httpx.AsyncClient(timeout=10) as client:
                                await client.post(
                                    f"https://api.telegram.org/bot{token}/sendMessage",
                                    json={"chat_id": chat_id, "text": _smart_truncate(result_text)},
                                )
                                await client.post(
                                    f"https://api.telegram.org/bot{token}/sendMessage",
                                    json={"chat_id": chat_id, "text": "..."},
                                )
                        except Exception as exc:
                            logger.warning("telegram batch task: failed to send result: %s", exc)
                    await _register_pending_reply(chat_id, tid, conversation_id=None)

                asyncio.create_task(_run_batch(_batch_thread_id, _batch_intent, _batch_file_context))
                return {"ok": True}

            # No automation pending — check if user is hinting a file is coming
            pending_file_row = await _get_and_clear_pending_file(chat_id)
            if _text_hints_file(text):
                active_thread_id = pending_file_row.get("thread_id") if pending_file_row else None
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
            elif pending_file_row:
                # User redirected away from file intent — run agent with new text
                redirect_thread_id = pending_file_row.get("thread_id")
                if not redirect_thread_id:
                    async with AsyncSessionLocal() as db:
                        new_thread = Thread(title=text[:60], model="gpt-4o-mini")
                        db.add(new_thread)
                        await db.commit()
                        await db.refresh(new_thread)
                        redirect_thread_id = new_thread.id
                if token:
                    try:
                        async with httpx.AsyncClient(timeout=5) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": "Got it. Working on it..."},
                            )
                    except Exception:
                        pass

                async def _run_redirect(tid: int, msg_text: str) -> None:
                    result_text = await _run_direct_thread(msg_text, tid)
                    new_pending = await _has_pending_reply(chat_id)
                    if not new_pending and token:
                        try:
                            async with httpx.AsyncClient(timeout=10) as client:
                                await client.post(
                                    f"https://api.telegram.org/bot{token}/sendMessage",
                                    json={"chat_id": chat_id, "text": _smart_truncate(result_text)},
                                )
                                await client.post(
                                    f"https://api.telegram.org/bot{token}/sendMessage",
                                    json={"chat_id": chat_id, "text": "..."},
                                )
                        except Exception as exc:
                            logger.warning("telegram webhook: redirect task failed to send result: %s", exc)
                        await _register_pending_reply(chat_id, tid, conversation_id=None)

                asyncio.create_task(_run_redirect(redirect_thread_id, text))
            return {"ok": True}

        conversation_id = pending.conversation_id
        pending_thread_id = pending.thread_id
        await db.execute(
            delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
        )
        await db.commit()

    # If user said "no"/"done"/etc., close the conversation gracefully.
    if _is_end_reply(text):
        logger.info("telegram webhook: end-of-conversation from chat_id=%s", chat_id)
        if token and chat_id:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": "Alright, talk to you later!"},
                    )
            except Exception:
                pass
        return {"ok": True}

    if conversation_id is None:
        # No AutomationConversation — this came from a direct chat thread.
        # Resume the LangGraph thread tied to that DB thread_id directly.
        db_thread_id = pending_thread_id
        logger.info(
            "telegram webhook: no conversation_id — resuming via db_thread_id=%s", db_thread_id
        )
        if token and chat_id:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": "Got it. Working on it..."},
                    )
            except Exception:
                pass

        async def _run_direct(db_tid: int) -> None:
            result_text = await _run_direct_thread(text, db_tid)
            # If the supervisor itself called telegram_ask, it already registered
            # a pending reply — don't double-register.
            new_pending = await _has_pending_reply(chat_id)
            if not new_pending and token and chat_id:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": _smart_truncate(result_text)},
                        )
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "..."},
                        )
                except Exception as exc:
                    logger.warning("telegram webhook: failed to send result: %s", exc)
                # Re-register pending so the next reply continues this thread
                await _register_pending_reply(chat_id, db_tid, conversation_id=None)

        asyncio.create_task(_run_direct(db_thread_id))
        return {"ok": True}

    # Send immediate acknowledgement
    if token and chat_id:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": "Got it. Working on it..."},
                )
        except Exception:
            pass

    async def _run_and_notify(conv_id: int) -> None:
        result = await _run_continuation(text, conv_id)
        # If the supervisor itself called telegram_ask, pending is already registered.
        new_pending = await _has_pending_reply(chat_id)
        if not new_pending and token and chat_id:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": _smart_truncate(result)},
                    )
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": "..."},
                    )
            except Exception as exc:
                logger.warning("telegram webhook: failed to send result: %s", exc)
            # Re-register pending so the next reply continues this conversation
            await _register_pending_reply(chat_id, pending_thread_id, conversation_id=conv_id)

    asyncio.create_task(_run_and_notify(conversation_id))

    return {"ok": True}
