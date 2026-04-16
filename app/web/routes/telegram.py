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
from app.db.models import Message, TelegramPendingReply, Thread

logger = logging.getLogger(__name__)


async def _download_telegram_file(token: str, message: dict) -> tuple[str, str] | None:
    """Download a file from a Telegram message to workspace/telegram_uploads/.

    Supports: document, photo (largest), audio, voice, video.
    Returns (filename, absolute_path) on success, None on failure.
    """
    import httpx
    import app.config as _cfg

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

            upload_dir = _cfg.WORKSPACE_DIR / "telegram_uploads"
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
    from app.db.models import TelegramPendingFile
    from sqlalchemy import delete as sa_delete

    async with AsyncSessionLocal() as db:
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
    from app.db.models import TelegramPendingReply
    from sqlalchemy import delete as sa_delete

    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        await db.execute(
            sa_delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
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


async def _run_direct_thread(user_reply: str, db_thread_id: int) -> str:
    """Resume a direct chat LangGraph thread (no AutomationConversation) with the user's reply.

    Used when telegram_ask was called from a plain chat thread rather than an automation.
    The LangGraph thread key is str(db_thread_id) — same as the chat route uses.
    """
    from langchain_core.messages import AIMessage, HumanMessage
    from app.agents.supervisor import get_graph

    lg_thread_id = str(db_thread_id)

    # Persist user reply into the DB thread so it shows in the UI
    async with AsyncSessionLocal() as db:
        thread = await db.get(Thread, db_thread_id)
        model = thread.model if thread else "gpt-4o"
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
            "model": "gpt-4o",
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
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return {"ok": True}

    token = app_config.TELEGRAM_BOT_TOKEN

    # ── /newthread [optional title] ────────────────────────────────────────
    if text.lower().startswith("/newthread"):
        title = text[len("/newthread"):].strip() or "New Chat"
        async with AsyncSessionLocal() as db:
            thread = Thread(title=title, model="gpt-4o")
            db.add(thread)
            await db.commit()
            await db.refresh(thread)
            new_thread_id = thread.id

        # Clear any existing pending reply and point to the new thread
        await _register_pending_reply(chat_id, new_thread_id, conversation_id=None)

        if token:
            try:
                import httpx
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
    if text.lower() == "/thread":
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
                import httpx
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": reply},
                    )
            except Exception:
                pass
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
                import httpx
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
                import httpx
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
                    import httpx
                    reply_body = result_text[:1000] + ("..." if len(result_text) > 1000 else "")
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": reply_body},
                        )
                        # Ask "Anything else?" and keep the loop open
                        await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": "Anything else?"},
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
            import httpx
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
                import httpx
                reply_body = result[:1000] + ("..." if len(result) > 1000 else "")
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
                logger.warning("telegram webhook: failed to send result: %s", exc)
            # Re-register pending so the next reply continues this conversation
            await _register_pending_reply(chat_id, pending_thread_id, conversation_id=conv_id)

    asyncio.create_task(_run_and_notify(conversation_id))

    return {"ok": True}
