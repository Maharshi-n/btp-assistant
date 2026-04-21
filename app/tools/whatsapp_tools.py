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
from app.tools.filesystem import _safe_resolve, OutsideWorkspaceError

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

    # Fire outgoing automation trigger (fire-and-forget)
    try:
        import asyncio as _asyncio
        from app.automations.runtime import on_whatsapp_outgoing
        _asyncio.get_running_loop().create_task(
            on_whatsapp_outgoing(chat_id=chat_id, message_text=text, group_name=group_name or "")
        )
    except Exception:
        pass

    label = group_name or chat_id
    return f"Sent to {label}."


class WhatsAppSendFileInput(BaseModel):
    chat_id: str = Field(
        description="Green API chat ID, e.g. '120363012345@g.us' for groups or '919876543210@c.us' for contacts."
    )
    file_path: str = Field(
        description="Absolute path to the local file to send (image, PDF, video, etc.)."
    )
    caption: str = Field(default="", description="Optional caption shown below the file.")


@tool(args_schema=WhatsAppSendFileInput)
async def whatsapp_send_file(chat_id: str, file_path: str, caption: str = "") -> str:
    """Send a local file (image, PDF, video) to a WhatsApp group or contact.

    Uploads the file directly from the local filesystem via Green API.
    Only registered groups are allowed for group chat_ids.
    """
    client = get_green_client()
    if client is None:
        return "WhatsApp not configured."

    # Resolve file path and verify it's inside the workspace
    try:
        p = _safe_resolve(file_path)
    except OutsideWorkspaceError as e:
        return str(e)

    if not p.is_file():
        return f"Path is not a file: {file_path}"

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
        await client.send_file_by_upload(chat_id, str(p), caption)
    except GreenAPIError as exc:
        logger.warning("whatsapp_send_file: API error: %s", exc)
        return f"WhatsApp send file failed: {exc}"

    try:
        async with AsyncSessionLocal() as db:
            msg = WhatsAppMessage(
                message_id=f"out_{uuid.uuid4().hex}",
                chat_id=chat_id,
                sender_id="agent",
                sender_name="RAION",
                direction="outgoing",
                message_type="file",
                text=caption or p.name,
            )
            db.add(msg)
            await db.commit()
    except Exception as exc:
        logger.warning("whatsapp_send_file: failed to persist: %s", exc)

    # Fire outgoing automation trigger (fire-and-forget)
    try:
        import asyncio as _asyncio
        from app.automations.runtime import on_whatsapp_outgoing
        _asyncio.get_running_loop().create_task(
            on_whatsapp_outgoing(chat_id=chat_id, message_text=caption or p.name, group_name=group_name or "")
        )
    except Exception:
        pass

    label = group_name or chat_id
    return f"File '{p.name}' sent to {label}."


class WhatsAppReadMessagesInput(BaseModel):
    chat_id: str = Field(
        description="Green API chat ID of the group or contact to read messages from."
    )
    count: int = Field(
        default=20,
        description="Number of recent messages to fetch (max 100).",
        ge=1,
        le=100,
    )


@tool(args_schema=WhatsAppReadMessagesInput)
async def whatsapp_read_messages(chat_id: str, count: int = 20) -> str:
    """Read recent messages from a WhatsApp group or contact chat.

    Returns the last N messages formatted as a readable transcript.
    """
    client = get_green_client()
    if client is None:
        return "WhatsApp not configured."

    try:
        messages = await client.get_chat_history(chat_id, count=count)
    except GreenAPIError as exc:
        return f"Failed to fetch messages: {exc}"

    if not messages:
        return "No messages found."

    lines: list[str] = []
    for m in messages:
        sender = m.get("senderName") or m.get("senderId", "unknown")
        msg_type = m.get("type", "text")
        timestamp = m.get("timestamp", "")
        if msg_type == "textMessage":
            text = m.get("textMessage", "")
        elif msg_type == "extendedTextMessage":
            text = m.get("extendedTextMessage", {}).get("text", "")
        elif msg_type in ("imageMessage", "videoMessage", "documentMessage"):
            caption = m.get(msg_type, {}).get("caption", "")
            text = f"[{msg_type.replace('Message','')}] {caption}".strip()
        else:
            text = f"[{msg_type}]"
        lines.append(f"[{timestamp}] {sender}: {text}")

    return "\n".join(lines)


@tool
async def whatsapp_get_groups() -> str:
    """List all WhatsApp groups registered in RAION with their chat IDs and names.

    Use this to look up a group's chat_id before calling whatsapp_send or whatsapp_read_messages.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WhatsAppGroup).order_by(WhatsAppGroup.name)
        )
        groups = result.scalars().all()

    if not groups:
        return "No WhatsApp groups registered. Add groups at /whatsapp."

    lines = [f"- {g.name}: {g.chat_id} ({'enabled' if g.enabled else 'disabled'})" for g in groups]
    return "Registered WhatsApp groups:\n" + "\n".join(lines)
