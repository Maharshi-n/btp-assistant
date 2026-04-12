"""Telegram notification tool — sends a message to the configured Telegram chat."""
from __future__ import annotations

import logging

import httpx
from langchain_core.tools import tool

import app.config as app_config

logger = logging.getLogger(__name__)


@tool
async def telegram_send(message: str) -> str:
    """Send a short notification message to the configured Telegram chat.

    Use this when the action_prompt explicitly asks you to notify or send
    a notification. Keep the message to 2-3 sentences, plain text only.
    Only call this once per automation run.

    Args:
        message: Plain text message to send. 2-3 sentences max, no markdown.

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
