"""Telegram notification tool — sends a message to the configured Telegram chat."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from langchain_core.tools import tool

import app.config as app_config

logger = logging.getLogger(__name__)


@tool
async def telegram_send(message: str) -> str:
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

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
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
async def telegram_ask(question: str, continuation_prompt: str) -> str:
    """Ask the user a question on Telegram and wait for their reply before continuing.

    Use this when the automation needs user input before it can proceed
    (e.g. asking what to reply to an email). The user's reply will resume
    the automation automatically.

    Args:
        question: The question to send to the user on Telegram.
        continuation_prompt: Full instructions for what to do with the user's reply.
            Include all context needed (email content, recipient, etc.) so the
            next step can run without any prior context.

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

    # Store pending reply in DB — upsert by chat_id (replace existing)
    try:
        from app.db.engine import AsyncSessionLocal
        from app.db.models import TelegramPendingReply
        from sqlalchemy import delete

        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        async with AsyncSessionLocal() as db:
            # Delete any existing pending reply for this chat_id
            await db.execute(
                delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
            )
            pending = TelegramPendingReply(
                chat_id=chat_id,
                continuation_prompt=continuation_prompt,
                thread_id=0,  # not tied to a specific thread — webhook creates its own
                expires_at=expires,
            )
            db.add(pending)
            await db.commit()
    except Exception as exc:
        logger.warning("telegram_ask: failed to store pending reply: %s", exc)
        return f"Question sent but failed to store pending reply: {exc}"

    logger.info("telegram_ask: question sent, pending reply stored for chat_id=%s", chat_id)
    return "Asked. Waiting for your Telegram reply."
