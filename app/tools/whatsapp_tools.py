"""WhatsApp send tool for the RAION agent."""
from __future__ import annotations

import logging
import uuid

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.engine import AsyncSessionLocal
from app.db.models import WhatsAppGroup, WhatsAppMessage
from app.integrations.green_api import GreenAPIError, get_green_client

logger = logging.getLogger(__name__)


class WhatsAppSendInput(BaseModel):
    chat_id: str = Field(
        description="Green API chat ID, e.g. '120363012345678901@g.us' for groups or '919876543210@c.us' for contacts."
    )
    text: str = Field(description="Message text to send.")


@tool(args_schema=WhatsAppSendInput)
async def whatsapp_send(chat_id: str, text: str) -> str:
    """Send a WhatsApp message to a registered group or direct contact.

    chat_id must be a Green API format ID. For groups, only use chat_ids that
    appear in the configured WhatsApp groups. For direct contacts (@c.us) the
    send is always allowed.
    """
    client = get_green_client()
    if client is None:
        return "WhatsApp not configured — GREEN_API_INSTANCE_ID and GREEN_API_TOKEN must be set."

    # Validate group chat_id against DB (direct @c.us contacts bypass this)
    group_name: str | None = None
    if chat_id.endswith("@g.us"):
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(WhatsAppGroup).where(WhatsAppGroup.chat_id == chat_id)
            )
            group = result.scalars().first()
        if group is None:
            return f"Group '{chat_id}' is not registered. Add it at /whatsapp first."
        group_name = group.name

    try:
        await client.send_message(chat_id, text)
    except GreenAPIError as exc:
        logger.warning("whatsapp_send: API error: %s", exc)
        return f"WhatsApp send failed: {exc}"

    # Persist outgoing message
    try:
        async with AsyncSessionLocal() as db:
            msg = WhatsAppMessage(
                message_id=f"out_{uuid.uuid4().hex}",
                chat_id=chat_id,
                sender_id="agent",
                sender_name="RAION",
                direction="outgoing",
                message_type="text",
                text=text,
            )
            db.add(msg)
            await db.commit()
    except Exception as exc:
        logger.warning("whatsapp_send: failed to persist outgoing message: %s", exc)

    label = group_name or chat_id
    return f"Sent to {label}."
