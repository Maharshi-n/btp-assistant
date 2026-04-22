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

from app.db.models import Message, TelegramPendingReply, Thread

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
            model=app_config.DEFAULT_THREAD_MODEL,
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

    # Human-friendly confirmation in IST
    ist = timezone(timedelta(hours=5, minutes=30))
    local_str = fire_at.astimezone(ist).strftime("%b %d at %I:%M %p IST")
    return f"Reminder set for {local_str}."


async def _fire_reminder(chat_id: str, token: str | None, message: str) -> None:
    """Fire a one-shot reminder by running the agent with the reminder text as the prompt.

    Creates a new thread, runs the supervisor, sends the result to Telegram.
    Does NOT register a pending reply or call telegram_ask — reminders are
    one-shot notifications. The user can /switch to the thread to follow up.
    Falls back to a plain text reminder if the agent run fails.
    """
    if not token:
        return

    thread_id: int = 0
    reply: str = f"Reminder: {message}"

    try:
        async with AsyncSessionLocal() as db:
            thread = Thread(title=f"Reminder: {message[:50]}", model=app_config.DEFAULT_THREAD_MODEL)
            db.add(thread)
            await db.commit()
            await db.refresh(thread)
            thread_id = thread.id

        # Instruct the agent to produce a result only — no follow-up questions,
        # no telegram_ask calls. The user can /switch to this thread to continue.
        tagged = (
            f"[via Telegram] [Reminder triggered — respond with result only, "
            f"do NOT call telegram_ask or ask follow-up questions] {message}"
        )
        result_text = await _run_direct_thread(tagged, thread_id)
        if result_text and result_text != "Done.":
            reply = _smart_truncate(result_text)
    except Exception as exc:
        logger.warning("_fire_reminder: agent run failed: %s", exc)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Send result + thread ID so user can /switch to follow up
            notification = f"{reply}\n\n[Thread #{thread_id} — /switch {thread_id} to follow up]"
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": notification},
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


_IDLE_TIMEOUT_SECONDS = 120  # close thread after 2 min of inactivity


def _idle_job_id(chat_id: str) -> str:
    return f"tg_idle_{chat_id}"


def _schedule_idle_close(chat_id: str, thread_id: int) -> None:
    """Schedule (or reschedule) a 2-min idle-close job for this chat."""
    try:
        from app.automations.runtime import get_scheduler
        from apscheduler.triggers.date import DateTrigger

        scheduler = get_scheduler()
        if scheduler is None or not scheduler.running:
            return

        fire_at = datetime.now(timezone.utc) + timedelta(seconds=_IDLE_TIMEOUT_SECONDS)
        scheduler.add_job(
            _fire_idle_close,
            trigger=DateTrigger(run_date=fire_at),
            id=_idle_job_id(chat_id),
            args=[chat_id, thread_id],
            replace_existing=True,
            max_instances=1,
        )
    except Exception as exc:
        logger.warning("_schedule_idle_close: failed: %s", exc)


def _cancel_idle_close(chat_id: str) -> None:
    """Cancel any pending idle-close job for this chat (user sent a message)."""
    try:
        from app.automations.runtime import get_scheduler

        scheduler = get_scheduler()
        if scheduler is None or not scheduler.running:
            return
        job = scheduler.get_job(_idle_job_id(chat_id))
        if job:
            job.remove()
    except Exception as exc:
        logger.warning("_cancel_idle_close: failed: %s", exc)


async def _fire_idle_close(chat_id: str, thread_id: int) -> None:
    """APScheduler job: clear the pending reply and notify the user."""
    token = app_config.TELEGRAM_BOT_TOKEN
    try:
        async with AsyncSessionLocal() as db:
            # Only close if the pending reply still points to this thread
            now = datetime.now(timezone.utc)
            result = await db.execute(
                select(TelegramPendingReply)
                .where(TelegramPendingReply.chat_id == chat_id)
                .where(TelegramPendingReply.expires_at > now)
            )
            active = result.scalars().first()
            if active is None or active.thread_id != thread_id:
                return  # thread already switched or closed
            await db.execute(
                delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
            )
            await db.commit()
    except Exception as exc:
        logger.warning("_fire_idle_close: db error: %s", exc)
        return

    if token:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": "Thread closed due to inactivity. Send a message anytime to start a new one."},
                )
        except Exception as exc:
            logger.warning("_fire_idle_close: send failed: %s", exc)

    logger.info("_fire_idle_close: closed idle thread #%s for chat %s", thread_id, chat_id)


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
    _schedule_idle_close(chat_id, thread_id)


async def _register_or_notify_clash(
    chat_id: str,
    thread_id: int,
    token: str | None,
    last_reply: str,
    conversation_id: int | None = None,
) -> None:
    """Register pending reply, or send a clash notification if a different thread is active.

    If no active conversation exists, or the active thread is the same thread,
    register normally. If a *different* thread is active, send a notification
    with the thread ID so the user can /switch later — don't overwrite their
    current conversation.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramPendingReply)
            .where(TelegramPendingReply.chat_id == chat_id)
            .where(TelegramPendingReply.expires_at > now)
        )
        active = result.scalars().first()

    if active is not None and active.thread_id != thread_id:
        # Different thread is active — notify without overwriting
        if token:
            try:
                async with AsyncSessionLocal() as db:
                    clash_thread = await db.get(Thread, thread_id)
                title = clash_thread.title if clash_thread else f"Thread #{thread_id}"
                notification = (
                    f"[{title} — Thread #{thread_id}]\n"
                    f"{_smart_truncate(last_reply, limit=800)}\n\n"
                    f"/switch {thread_id} to continue this when ready."
                )
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": notification},
                    )
            except Exception as exc:
                logger.warning("_register_or_notify_clash: failed to send notification: %s", exc)
        return

    # No clash — register normally
    await _register_pending_reply(chat_id, thread_id, conversation_id=conversation_id)


async def _notify_thread_created(chat_id: str, thread_id: int, token: str | None) -> None:
    """Send a brief thread ID notice when a new thread is created for this chat."""
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"[Thread #{thread_id}]"},
            )
    except Exception as exc:
        logger.warning("_notify_thread_created: failed: %s", exc)


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

    # Use a separate LangGraph checkpoint namespace for Telegram threads so that
    # broken web-UI state (dangling tool calls, corrupt windows) never contaminates
    # Telegram runs and vice versa.
    lg_thread_id = f"tg_{db_thread_id}"

    # Tag the message as coming from Telegram — silent context for the agent
    tagged_reply = f"[via Telegram] {user_reply}"

    # Persist user reply into the DB thread so it shows in the UI (without the tag)
    async with AsyncSessionLocal() as db:
        thread = await db.get(Thread, db_thread_id)
        model = thread.model if thread else app_config.DEFAULT_THREAD_MODEL
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
        # LangGraph default recursion_limit is 25. For tool-heavy tasks
        # (especially flaky ones like Playwright with retries) that's too low
        # and triggers GraphRecursionError before our own bounds fire.
        # Our supervisor enforces MAX_TOOL_CALLS=50 + 10-min wall clock, so
        # 100 here is just an outer safety net.
        "recursion_limit": 100,
        "configurable": {
            "thread_id": lg_thread_id,
            "ws_thread_id": db_thread_id,
            "model": model,
            "automation_run": True,
        },
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
        # Wipe the broken tg_ checkpoint so the next message starts with clean state
        try:
            from app.agents.supervisor import get_graph
            checkpointer = get_graph().checkpointer
            if checkpointer is not None:
                await checkpointer.adelete_thread(lg_thread_id)
                logger.info("_run_direct_thread: cleared broken checkpoint for %s", lg_thread_id)
        except Exception as ce:
            logger.warning("_run_direct_thread: failed to clear checkpoint: %s", ce)
        # Friendly message for the most common failure mode (recursion/retry loop)
        exc_str = str(exc)
        if "Recursion limit" in exc_str or "GraphRecursion" in type(exc).__name__:
            return (
                "I got stuck in a retry loop and had to stop. This usually "
                "means a tool (often Playwright) kept failing. Try again in "
                "a moment — if it keeps happening, go to /connectors and click "
                "Refresh on the relevant connector."
            )
        return f"Something went wrong: {exc_str[:300]}"


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
        "recursion_limit": 100,
        "configurable": {
            "thread_id": lg_thread_id,       # same thread — LLM sees full history
            "ws_thread_id": db_thread_id or 0,
            "model": app_config.DEFAULT_THREAD_MODEL,
            "automation_run": True,
        },
    }

    # Prefix with Telegram context so the LLM knows this is a follow-up reply
    # from the user in the active Telegram conversation — not a new automation trigger.
    # This prevents the LLM from re-running the original automation action (e.g. calling
    # telegram_send with the same notification) instead of responding to the user's request.
    tagged_reply = f"[via Telegram — user follow-up reply] {user_reply}"
    lc_messages = [HumanMessage(content=tagged_reply)]
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

    # Cancel any pending idle-close job — user is active
    _cancel_idle_close(chat_id)

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
                        json={"chat_id": chat_id, "text": transcript},
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

        # Intent: caption if present, otherwise a neutral prompt asking the agent
        # to inspect the file. No more two-step pending-file flow.
        intent = text if text else f"The user uploaded a file named {filename}. Look at it and ask them what they want done, or act on it if the content makes the intent obvious."
        all_files = [{"filename": filename, "file_path": file_path}]

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

        _new_file_thread = False
        if not thread_id:
            async with AsyncSessionLocal() as db:
                thread_title = (intent or filename)[:60]
                new_thread = Thread(title=thread_title, model=app_config.DEFAULT_THREAD_MODEL)
                db.add(new_thread)
                await db.commit()
                await db.refresh(new_thread)
                thread_id = new_thread.id
            _new_file_thread = True

        if token:
            try:
                ack = f"Got it, working on it... [Thread #{thread_id}]" if _new_file_thread else "Got it, working on it..."
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": ack},
                    )
            except Exception:
                pass

        async def _run_file_task(tid: int, p: str, fc: str) -> None:
            _cancel_idle_close(chat_id)
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
            await _register_or_notify_clash(chat_id, tid, token, result_text, conversation_id=None)

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
            thread = Thread(title=title, model=app_config.DEFAULT_THREAD_MODEL)
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
                            "text": f"[Thread #{new_thread_id}] Hey! How can I help you?",
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
            "/threads — list last 5 threads\n"
            "/switch <id> — switch to a parked thread\n"
            "/model [name] — view or change AI model\n"
            "/remember <fact> — save something to memory\n"
            "/ls [folder] — list workspace files\n"
            "/remind <when> <what> — set a one-off reminder\n"
            "  e.g. /remind at 5pm review the report\n"
            "  e.g. /remind in 30 minutes check email\n"
            "/automation <description> — create an automation\n"
            "  e.g. /automation send me weather every morning at 8am\n\n"
            "Send a file anytime — I'll ask what to do with it.\n"
            "Send multiple files then type 'done' to process them all together.\n"
            "Send a voice note — it'll be transcribed and treated as a message."
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

    # ── /threads — list last 5 threads ───────────────────────────────────────
    if _text_norm.lower() in ("/threads", "/threads "):
        async with AsyncSessionLocal() as db:
            _tlist_result = await db.execute(
                select(Thread).order_by(Thread.created_at.desc()).limit(5)
            )
            _recent_threads = _tlist_result.scalars().all()

        if not _recent_threads:
            reply = "No threads yet. Send /newthread to create one."
        else:
            lines = []
            now_check_t = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                _active_tpr_r = await db.execute(
                    select(TelegramPendingReply)
                    .where(TelegramPendingReply.chat_id == chat_id)
                    .where(TelegramPendingReply.expires_at > now_check_t)
                )
                _active_tpr = _active_tpr_r.scalars().first()
            active_tid = _active_tpr.thread_id if _active_tpr else None

            for t in _recent_threads:
                marker = " ◀ active" if t.id == active_tid else ""
                lines.append(f"#{t.id} — {t.title[:40]}{marker}")
            reply = "Last 5 threads:\n" + "\n".join(lines) + "\n\nUse /switch <id> to resume one."

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

    # ── /switch <thread_id> — resume a parked automation thread ──────────────
    if _text_norm.lower().startswith("/switch"):
        _switch_arg = _text_norm[len("/switch"):].strip()
        if not _switch_arg.isdigit():
            reply = "Usage: /switch <thread_id>\nExample: /switch 42"
        else:
            _switch_tid = int(_switch_arg)
            async with AsyncSessionLocal() as db:
                _switch_thread = await db.get(Thread, _switch_tid)
            if _switch_thread is None:
                reply = f"Thread #{_switch_tid} not found."
            else:
                # Fetch last AI message from that thread
                async with AsyncSessionLocal() as db:
                    from sqlalchemy import desc
                    from app.db.models import AutomationConversation
                    _last_msg_result = await db.execute(
                        select(Message)
                        .where(Message.thread_id == _switch_tid)
                        .where(Message.role == "assistant")
                        .order_by(desc(Message.created_at))
                        .limit(1)
                    )
                    _last_msg = _last_msg_result.scalars().first()

                    # Look up the AutomationConversation for this thread so replies
                    # resume the correct LangGraph checkpoint (not a fresh tg_ one)
                    _conv_result = await db.execute(
                        select(AutomationConversation)
                        .where(AutomationConversation.db_thread_id == _switch_tid)
                        .where(AutomationConversation.status == "active")
                        .order_by(desc(AutomationConversation.created_at))
                        .limit(1)
                    )
                    _switch_conv = _conv_result.scalars().first()
                    _switch_conv_id = _switch_conv.id if _switch_conv else None

                # Register this thread as active, preserving conversation_id
                await _register_pending_reply(chat_id, _switch_tid, conversation_id=_switch_conv_id)

                last_text = _last_msg.content if _last_msg else "(no messages yet)"
                reply = (
                    f"Switched to Thread #{_switch_tid}: \"{_switch_thread.title}\"\n\n"
                    f"Last message:\n{_smart_truncate(last_text, limit=800)}"
                )

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

    # ── /automation <description> — create an automation from Telegram ───────
    if _text_norm.lower().startswith("/automation"):
        description = _text_norm[len("/automation"):].strip()
        if not description:
            reply = "Usage: /automation <description>\nExample: /automation send me weather every morning at 8am"
        else:
            try:
                from app.automations.parser import parse_automation
                from app.automations.runtime import register_new_automation
                from app.db.models import Automation
                import json as _json

                async with AsyncSessionLocal() as db:
                    parsed = await parse_automation(description, db=db)
                    automation = Automation(
                        name=parsed["name"],
                        trigger_type=parsed["trigger_type"],
                        trigger_config_json=_json.dumps(parsed["trigger_config"]),
                        action_prompt=parsed["action_prompt"],
                        model=app_config.DEFAULT_THREAD_MODEL,
                        enabled=True,
                    )
                    db.add(automation)
                    await db.commit()
                    await db.refresh(automation)

                await register_new_automation(automation)

                trigger_display = parsed["trigger_type"]
                if parsed["trigger_type"] == "cron":
                    trigger_display = f"cron: {parsed['trigger_config'].get('cron', '')}"
                elif parsed["trigger_type"] == "gmail_new_from_sender":
                    trigger_display = f"gmail from {parsed['trigger_config'].get('sender', '')}"
                elif parsed["trigger_type"] == "gmail_any_new":
                    trigger_display = "any new gmail"
                elif parsed["trigger_type"] == "gmail_keyword_match":
                    trigger_display = f"gmail keyword: {parsed['trigger_config'].get('keywords', '')}"
                elif parsed["trigger_type"] == "fs_new_in_folder":
                    trigger_display = f"new file in {parsed['trigger_config'].get('folder', '')}"

                reply = (
                    f"Automation created: {parsed['name']}\n"
                    f"Trigger: {trigger_display}\n"
                    f"ID: #{automation.id}"
                )
            except ValueError as exc:
                reply = f"Could not parse automation: {exc}"
            except Exception as exc:
                logger.exception("telegram /automation: failed: %s", exc)
                reply = "Something went wrong creating the automation. Please try again."

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
                    model=_custom_cmd.model,
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
                            json={"chat_id": chat_id, "text": f"Got it, working on it... [Thread #{_cmd_thread_id}]"},
                        )
                except Exception:
                    pass

            async def _run_custom_cmd(tid: int, prompt: str) -> None:
                _cancel_idle_close(chat_id)
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
                await _register_or_notify_clash(chat_id, tid, token, result_text, conversation_id=None)

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
            _new_cmd_thread = False
            if not _cmd_thread_id:
                async with AsyncSessionLocal() as db:
                    _ct = Thread(title=f"/{_cmd_name}", model=_custom_cmd.model)
                    db.add(_ct)
                    await db.commit()
                    await db.refresh(_ct)
                    _cmd_thread_id = _ct.id
                _new_cmd_thread = True

            if token:
                try:
                    ack = f"Got it, working on it... [Thread #{_cmd_thread_id}]" if _new_cmd_thread else "Got it, working on it..."
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": ack},
                        )
                except Exception:
                    pass

            async def _run_custom_cmd(tid: int, prompt: str) -> None:
                _cancel_idle_close(chat_id)
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
                await _register_or_notify_clash(chat_id, tid, token, result_text, conversation_id=None)

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
            # Plain new message — no active thread, no file. Create a new thread.
            async with AsyncSessionLocal() as db:
                new_thread = Thread(title=text[:60], model=app_config.DEFAULT_THREAD_MODEL)
                db.add(new_thread)
                await db.commit()
                await db.refresh(new_thread)
                new_msg_thread_id = new_thread.id

            if token:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": f"Got it, working on it... [Thread #{new_msg_thread_id}]"},
                        )
                except Exception:
                    pass

            async def _run_new_thread(tid: int) -> None:
                _cancel_idle_close(chat_id)
                result_text = await _run_direct_thread(text, tid)
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
                        logger.warning("telegram webhook: new thread task failed: %s", exc)
                await _register_or_notify_clash(chat_id, tid, token, result_text, conversation_id=None)

            asyncio.create_task(_run_new_thread(new_msg_thread_id))
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
            _cancel_idle_close(chat_id)
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
                await _register_or_notify_clash(chat_id, db_tid, token, result_text, conversation_id=None)

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
        _cancel_idle_close(chat_id)
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
            await _register_or_notify_clash(chat_id, pending_thread_id, token, result, conversation_id=conv_id)

    asyncio.create_task(_run_and_notify(conversation_id))

    return {"ok": True}
