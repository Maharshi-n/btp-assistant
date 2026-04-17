"""Telegram notification tool — sends a message to the configured Telegram chat."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

import app.config as app_config
from app.tools.filesystem import OutsideWorkspaceError, _safe_resolve

logger = logging.getLogger(__name__)


@tool
async def telegram_send(message: str, config: RunnableConfig = None) -> str:
    """Send a message to the user's Telegram chat.

    Args:
        message: The text message to send.

    Returns:
        "Sent." on success, or an error description.
    """
    token = app_config.TELEGRAM_BOT_TOKEN
    chat_id = app_config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        logger.warning("telegram_send called but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured")
        return "Telegram not configured — skipping notification."

    # Resolve thread_id from LangGraph config
    cfg = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id_raw = cfg.get("ws_thread_id") or cfg.get("thread_id") or 0
    try:
        thread_id = int(thread_id_raw)
    except (TypeError, ValueError):
        thread_id = 0

    # If the user has an active conversation, append thread ID so they know
    # where this notification came from and can /switch to it later.
    text_to_send = message
    try:
        from app.db.engine import AsyncSessionLocal
        from app.db.models import TelegramPendingReply
        from sqlalchemy import select
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(TelegramPendingReply)
                .where(TelegramPendingReply.chat_id == chat_id)
                .where(TelegramPendingReply.expires_at > now)
            )
            active = existing.scalars().first()
        logger.info(
            "telegram_send: thread_id=%s active_thread=%s",
            thread_id, active.thread_id if active else None,
        )
        if active is not None and active.thread_id != thread_id:
            text_to_send = f"{message}\n\n[Thread #{thread_id} — /switch {thread_id} to follow up]"
    except Exception as exc:
        logger.warning("telegram_send: clash check failed: %s", exc)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text_to_send})
            if resp.status_code == 200:
                logger.info("Telegram notification sent successfully")
                return "Sent."
            else:
                body = resp.text[:300]
                logger.warning("Telegram API error %d: %s", resp.status_code, body)
                return f"Telegram API error {resp.status_code}: {body}"
    except Exception as exc:
        logger.warning("telegram_send failed: %s", exc)
        return f"Failed to send Telegram notification: {exc}"


@tool
async def telegram_ask(
    question: str,
    continuation_prompt: str,
    conversation_id: int | None = None,
    config: RunnableConfig = None,
) -> str:
    """Ask the user a question on Telegram and wait for their reply before continuing.

    Use this when the automation needs user input before it can proceed
    (e.g. asking what to reply to an email). The user's reply will resume
    the automation automatically.

    Args:
        question: The question to send to the user on Telegram.
        continuation_prompt: Full instructions for what to do with the user's reply.
        conversation_id: Optional ID of an AutomationConversation row holding
            structured state (sender email, subject, body, current draft).
            When provided, the webhook will load it and inject real values
            into the continuation — the LLM does NOT need to copy sender/
            subject/body through the prompt text.

    Returns:
        "Asked. Waiting for your Telegram reply." on success, or an error string.
    """
    token = app_config.TELEGRAM_BOT_TOKEN
    chat_id = app_config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        return "Telegram not configured — cannot ask question."

    # Send the question to Telegram
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": question})
            if resp.status_code != 200:
                return f"Telegram API error {resp.status_code}: {resp.text[:300]}"
    except Exception as exc:
        return f"Failed to send question via Telegram: {exc}"

    # Resolve real thread_id from LangGraph config (injected by LangChain)
    cfg = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id_raw = cfg.get("ws_thread_id") or cfg.get("thread_id") or 0
    try:
        thread_id = int(thread_id_raw)
    except (TypeError, ValueError):
        thread_id = 0

    # Check if there's already an active conversation with the user.
    # If yes, don't overwrite it — send a notification instead so the user
    # can /switch to this thread when they're done with the current one.
    try:
        from app.db.engine import AsyncSessionLocal
        from app.db.models import TelegramPendingReply
        from sqlalchemy import delete, select

        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(TelegramPendingReply)
                .where(TelegramPendingReply.chat_id == chat_id)
                .where(TelegramPendingReply.expires_at > now)
            )
            active = existing.scalars().first()

        if active is not None:
            # Another conversation is active — notify without taking over
            notification = (
                f"[Automation — Thread #{thread_id}]\n"
                f"{question}\n\n"
                f"Reply /switch {thread_id} to continue this when you're ready."
            )
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": notification},
                    )
            except Exception as exc:
                logger.warning("telegram_ask: failed to send clash notification: %s", exc)
            logger.info(
                "telegram_ask: active conversation detected — sent notification for thread %d", thread_id
            )
            return "Asked. Waiting for your Telegram reply."

        # No active conversation — register pending reply normally
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
            )
            pending = TelegramPendingReply(
                chat_id=chat_id,
                continuation_prompt="",   # unused — kept for schema compat
                last_question=question,   # stored so UI can show what was asked
                thread_id=thread_id,
                conversation_id=conversation_id,
                expires_at=expires,
            )
            db.add(pending)
            await db.commit()
    except Exception as exc:
        logger.warning("telegram_ask: failed to store pending reply: %s", exc)
        return f"Question sent but failed to store pending reply: {exc}"

    logger.info("telegram_ask: sent, conversation_id=%s", conversation_id)
    return "Asked. Waiting for your Telegram reply."


@tool
async def schedule_message(message: str, delay_seconds: int, config: RunnableConfig = None) -> str:
    """Schedule a Telegram message to be sent after a delay.

    Use this when the user asks to be reminded, woken up, or notified after
    a specific amount of time (e.g. "wake me up in 5 minutes", "remind me
    in 30 minutes"). The job is one-shot and fires exactly once.

    Note: scheduled jobs are lost if the server restarts before they fire.

    Args:
        message: The text to send on Telegram when the timer fires.
        delay_seconds: How many seconds from now to wait before sending.

    Returns:
        Confirmation string with the scheduled fire time, or an error.
    """
    from apscheduler.triggers.date import DateTrigger
    from datetime import timedelta
    from app.automations.runtime import get_scheduler

    token = app_config.TELEGRAM_BOT_TOKEN
    chat_id = app_config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        return "Telegram not configured — cannot schedule message."

    scheduler = get_scheduler()
    if scheduler is None or not scheduler.running:
        return "Scheduler not running — cannot schedule message."

    if delay_seconds < 1:
        return "delay_seconds must be at least 1."

    # Capture thread_id now (from LangGraph config) so the scheduled job can
    # append it when it fires — at fire time there's no config available.
    cfg = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id_raw = cfg.get("ws_thread_id") or cfg.get("thread_id") or 0
    try:
        source_thread_id = int(thread_id_raw)
    except (TypeError, ValueError):
        source_thread_id = 0

    fire_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    job_id = f"scheduled_msg_{fire_at.timestamp()}"

    async def _send() -> None:
        try:
            import httpx
            text_to_send = message
            if source_thread_id:
                try:
                    from app.db.engine import AsyncSessionLocal
                    from app.db.models import TelegramPendingReply
                    from sqlalchemy import select
                    now = datetime.now(timezone.utc)
                    async with AsyncSessionLocal() as db:
                        existing = await db.execute(
                            select(TelegramPendingReply)
                            .where(TelegramPendingReply.chat_id == chat_id)
                            .where(TelegramPendingReply.expires_at > now)
                        )
                        active = existing.scalars().first()
                    if active is not None and active.thread_id != source_thread_id:
                        text_to_send = f"{message}\n\n[Thread #{source_thread_id} — /switch {source_thread_id} to follow up]"
                except Exception:
                    pass
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text_to_send},
                )
            logger.info("schedule_message: fired job %s", job_id)
        except Exception as exc:
            logger.warning("schedule_message: failed to send: %s", exc)

    scheduler.add_job(
        _send,
        trigger=DateTrigger(run_date=fire_at),
        id=job_id,
        replace_existing=True,
        max_instances=1,
    )

    # Human-readable confirmation
    if delay_seconds < 60:
        when = f"{delay_seconds} second(s)"
    elif delay_seconds < 3600:
        when = f"{delay_seconds // 60} minute(s)"
    else:
        h, m = divmod(delay_seconds // 60, 60)
        when = f"{h}h {m}m" if m else f"{h}h"

    logger.info("schedule_message: scheduled job %s in %s", job_id, when)
    return f"Scheduled. I'll send '{message}' in {when} (at {fire_at.strftime('%H:%M:%S UTC')})."


@tool
async def save_draft(conversation_id: int, draft: str) -> str:
    """Save the current draft to the conversation's state so later rounds can retrieve it.

    Call this BEFORE telegram_ask whenever you show the user a draft for approval,
    so the next round (after their reply) has access to the exact draft text via
    the TRUSTED CONVERSATION CONTEXT block.

    Args:
        conversation_id: The conversation ID from the TRUSTED TRIGGER CONTEXT block.
        draft: The full draft text to save.
    """
    try:
        from app.automations.conversations import update_state
        await update_state(conversation_id, current_draft=draft)
        return "Draft saved."
    except Exception as exc:
        return f"Failed to save draft: {exc}"


@tool
async def telegram_send_file(path: str) -> str:
    """Send a file from the workspace to the user's Telegram chat.

    Sends the actual file bytes — does NOT read or summarize the content.
    Use this when the user asks to send a file, PDF, document, or image to Telegram.

    Args:
        path: Path to the file (relative to workspace or absolute).

    Returns:
        "Sent." on success, or an error description.
    """
    token = app_config.TELEGRAM_BOT_TOKEN
    chat_id = app_config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        return "Telegram not configured — cannot send file."

    try:
        resolved = _safe_resolve(path)
    except OutsideWorkspaceError as exc:
        return str(exc)

    if not resolved.exists():
        return f"File not found: {path}"
    if resolved.is_dir():
        return f"Path is a directory, not a file: {path}"

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        file_bytes = resolved.read_bytes()
        filename = resolved.name
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                data={"chat_id": chat_id},
                files={"document": (filename, file_bytes, "application/octet-stream")},
            )
            if resp.status_code == 200:
                logger.info("telegram_send_file: sent %s", filename)
                return "Sent."
            else:
                body = resp.text[:300]
                logger.warning("telegram_send_file API error %d: %s", resp.status_code, body)
                return f"Telegram API error {resp.status_code}: {body}"
    except Exception as exc:
        logger.warning("telegram_send_file failed: %s", exc)
        return f"Failed to send file via Telegram: {exc}"
