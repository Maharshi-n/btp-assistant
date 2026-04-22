"""WhatsApp send tool for the RAION agent."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import select, and_

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
        # Green API uses "typeMessage" for message type, "type" for direction (incoming/outgoing)
        msg_type = m.get("typeMessage") or m.get("type", "text")
        direction = m.get("type", "")
        timestamp = m.get("timestamp", "")
        if msg_type == "textMessage":
            text = m.get("textMessage", "")
        elif msg_type == "extendedTextMessage":
            text = (
                m.get("extendedTextMessage", {}).get("text", "")
                or m.get("textMessage", "")
            )
        elif msg_type in ("imageMessage", "videoMessage", "documentMessage"):
            inner = m.get(msg_type, {})
            caption = inner.get("caption", "") if isinstance(inner, dict) else ""
            text = f"[{msg_type.replace('Message', '')}] {caption}".strip()
        elif msg_type == "audioMessage":
            text = "[audio]"
        elif msg_type == "stickerMessage":
            text = "[sticker]"
        else:
            text = f"[{msg_type}]"
        prefix = f"({'out' if direction == 'outgoing' else 'in'})"
        lines.append(f"[{timestamp}] {prefix} {sender}: {text}")

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


class WhatsAppFetchMessagesInput(BaseModel):
    chat_id: str = Field(
        default="",
        description=(
            "Green API chat ID of a specific group/contact to fetch from. "
            "Leave empty ('') to fetch from ALL registered groups."
        ),
    )
    hours_back: float = Field(
        default=3.0,
        description="How many hours back to fetch messages. Ignored when since_midnight=True.",
        gt=0,
        le=720,
    )
    since_midnight: bool = Field(
        default=False,
        description=(
            "If True, fetch messages from midnight today (IST/local server time) until now. "
            "Overrides hours_back. Use for 'today', 'today's messages', etc."
        ),
    )
    limit: int = Field(
        default=500,
        description="Maximum total messages to return across all groups.",
        ge=1,
        le=2000,
    )


@tool(args_schema=WhatsAppFetchMessagesInput)
async def whatsapp_fetch_messages(
    chat_id: str = "",
    hours_back: float = 3.0,
    since_midnight: bool = False,
    limit: int = 500,
) -> str:
    """Fetch WhatsApp messages stored in the local database for a given time window.

    Unlike whatsapp_read_messages (which calls the Green API), this reads from
    RAION's own database — so it includes both incoming and outgoing messages,
    works for any time range, and never misses messages due to API limits.

    Use this for:
    - Summarizing group chats over a period ("last 3 hours", "today")
    - Automation reports across multiple groups
    - Queries like "messages that came today from XYZ group"

    Returns messages grouped by group name, with sender, direction, and timestamp.
    Returns a clear "No messages" response if the window is empty — safe for automations
    that should skip writing logs when nothing happened.
    """
    now = datetime.now(timezone.utc)

    if since_midnight:
        # Midnight in local server time expressed as UTC
        local_now = datetime.now()
        midnight_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight_local.astimezone(timezone.utc)
    else:
        since = now - timedelta(hours=hours_back)

    async with AsyncSessionLocal() as db:
        # Build group name lookup: chat_id → name
        grp_result = await db.execute(select(WhatsAppGroup))
        groups = grp_result.scalars().all()
        group_names: dict[str, str] = {g.chat_id: g.name for g in groups}

        # Determine which chat_ids to query
        if chat_id:
            target_ids = [chat_id]
        else:
            # All registered groups (only enabled ones for reporting)
            target_ids = [g.chat_id for g in groups if g.enabled]

        if not target_ids:
            return "No WhatsApp groups registered or enabled. Add groups at /whatsapp."

        # Query messages in the time window
        conditions = [
            WhatsAppMessage.chat_id.in_(target_ids),
            WhatsAppMessage.created_at >= since,
            WhatsAppMessage.created_at <= now,
        ]
        msg_result = await db.execute(
            select(WhatsAppMessage)
            .where(and_(*conditions))
            .order_by(WhatsAppMessage.chat_id, WhatsAppMessage.created_at)
            .limit(limit)
        )
        messages = msg_result.scalars().all()

    if not messages:
        window_desc = "today" if since_midnight else f"the last {hours_back:.4g}h"
        if chat_id:
            label = group_names.get(chat_id, chat_id)
            return f"No messages from '{label}' in {window_desc}."
        return f"No messages from any group in {window_desc}."

    # Group messages by chat_id for readable output
    from collections import defaultdict
    grouped: dict[str, list[WhatsAppMessage]] = defaultdict(list)
    for m in messages:
        grouped[m.chat_id].append(m)

    window_desc = "today (since midnight)" if since_midnight else f"last {hours_back:.4g}h"
    sections: list[str] = [
        f"WhatsApp messages — {window_desc} | total: {len(messages)}\n"
    ]

    for cid, msgs in grouped.items():
        group_label = group_names.get(cid, cid)
        sections.append(f"━━ {group_label} ({len(msgs)} messages) ━━")
        for m in msgs:
            ts = m.created_at.strftime("%H:%M") if m.created_at else "?"
            direction = "→" if m.direction == "outgoing" else "←"
            sender = m.sender_name or m.sender_id or "unknown"
            text = m.text or f"[{m.message_type}]"
            sections.append(f"  {ts} {direction} {sender}: {text}")
        sections.append("")

    return "\n".join(sections)
