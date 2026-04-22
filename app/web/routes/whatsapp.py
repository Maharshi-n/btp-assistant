"""WhatsApp integration routes — webhook, group CRUD, manual send, management UI."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.db.engine import AsyncSessionLocal, get_db
from app.db.models import WhatsAppGroup, WhatsAppMessage
from app.integrations.green_api import GreenAPIError, get_green_client
from app.web.deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


# ---------------------------------------------------------------------------
# Management UI page
# ---------------------------------------------------------------------------

@router.get("/whatsapp", response_class=HTMLResponse)
async def whatsapp_page(request: Request, _user=Depends(require_user), db: AsyncSession = Depends(get_db)):
    groups_result = await db.execute(select(WhatsAppGroup).order_by(WhatsAppGroup.created_at.desc()))
    groups = groups_result.scalars().all()

    msgs_result = await db.execute(
        select(WhatsAppMessage)
        .order_by(desc(WhatsAppMessage.created_at))
        .limit(20)
    )
    recent_messages = msgs_result.scalars().all()

    status = {"stateInstance": "unknown"}
    webhook_base_url = ""
    client = get_green_client()
    if client:
        try:
            status = await client.get_state()
        except Exception:
            pass
        try:
            settings = await client._get("getSettings")
            full_url: str = settings.get("webhookUrl", "")
            if full_url.endswith("/webhook/whatsapp"):
                webhook_base_url = full_url[: -len("/webhook/whatsapp")]
        except Exception:
            pass

    return templates.TemplateResponse("whatsapp.html", {
        "request": request,
        "groups": groups,
        "recent_messages": recent_messages,
        "instance_status": status.get("stateInstance", "unknown"),
        "whatsapp_enabled": app_config.whatsapp_enabled(),
        "webhook_base_url": webhook_base_url,
    })


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    """Receive Green API webhook events."""
    expected_token = app_config.GREEN_API_WEBHOOK_TOKEN
    if expected_token:
        bearer = (authorization or "").removeprefix("Bearer ").strip()
        if bearer != expected_token:
            raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    webhook_type = body.get("typeWebhook", "")

    if webhook_type == "stateInstanceChanged":
        state = body.get("stateInstance", "")
        logger.info("WhatsApp state changed: %s", state)
        if state == "notAuthorized":
            _notify_owner_telegram("WhatsApp instance is not authorized. Please re-scan the QR code at /whatsapp.")
        return {"ok": True}

    if webhook_type in ("outgoingAPIMessageReceived", "outgoingMessageReceived"):
        await _store_message(body, direction="outgoing")
        return {"ok": True}

    if webhook_type == "incomingMessageReceived":
        await _handle_incoming(body)

    return {"ok": True}


async def _handle_incoming(body: dict) -> None:
    sender_data = body.get("senderData", {})
    message_data = body.get("messageData", {})

    message_id: str = body.get("idMessage", f"unknown_{uuid.uuid4().hex}")
    chat_id: str = sender_data.get("chatId", "")
    sender_id: str = sender_data.get("sender", "")
    sender_name: str = sender_data.get("senderName", "")

    text: str = (
        message_data.get("textMessageData", {}).get("textMessage", "")
        or message_data.get("extendedTextMessageData", {}).get("text", "")
        or message_data.get("imageMessage", {}).get("caption", "")
        or message_data.get("videoMessage", {}).get("caption", "")
        or message_data.get("documentMessage", {}).get("caption", "")
        or ""
    )
    media_url: str = (
        message_data.get("imageMessage", {}).get("downloadUrl", "")
        or message_data.get("videoMessage", {}).get("downloadUrl", "")
        or message_data.get("documentMessage", {}).get("downloadUrl", "")
        or ""
    )

    # Deduplicate
    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(WhatsAppMessage).where(WhatsAppMessage.message_id == message_id)
        )
        if existing.scalars().first() is not None:
            logger.debug("WhatsApp webhook: duplicate message_id=%s — skipped", message_id)
            return

    # Look up group
    async with AsyncSessionLocal() as db:
        group_result = await db.execute(
            select(WhatsAppGroup).where(WhatsAppGroup.chat_id == chat_id)
        )
        group = group_result.scalars().first()

    # Store message regardless of whether group is registered/enabled
    async with AsyncSessionLocal() as db:
        msg = WhatsAppMessage(
            message_id=message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name or None,
            direction="incoming",
            message_type=_detect_message_type(message_data),
            text=text or None,
            media_url=media_url or None,
            raw_json=json.dumps(body),
        )
        db.add(msg)
        await db.commit()

    # Mark as seen in the polling seen-set so the poller doesn't fire automations again
    try:
        from app.automations.runtime import _wa_seen_ids, _wa_seen_lock
        async with _wa_seen_lock:
            _wa_seen_ids.setdefault(chat_id, set()).add(message_id)
    except Exception:
        pass

    if group is None or not group.enabled:
        return

    # Update last_message_at
    async with AsyncSessionLocal() as db:
        g = await db.get(WhatsAppGroup, group.id)
        if g:
            g.last_message_at = datetime.now(timezone.utc)
            await db.commit()

    # Keyword notification
    if text and group.keyword_filter:
        keywords = [k.strip().lower() for k in group.keyword_filter.split(",") if k.strip()]
        text_lower = text.lower()
        matched = next((k for k in keywords if k in text_lower), None)
        if matched:
            excerpt = text[:200]
            _notify_owner_telegram(
                f"Keyword '{matched}' matched in [{group.name}]:\n{excerpt}"
            )

    # Automation trigger
    try:
        from app.automations.runtime import on_whatsapp_message
        asyncio.get_running_loop().create_task(
            on_whatsapp_message(
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name=sender_name,
                message_text=text,
                group_name=group.name if group else "",
            )
        )
    except Exception as exc:
        logger.warning("WhatsApp automation dispatch failed: %s", exc)


async def _store_message(body: dict, direction: str) -> None:
    """Store an outgoing webhook event as a WhatsAppMessage (best-effort)."""
    try:
        message_id = body.get("idMessage", f"out_{uuid.uuid4().hex}")
        sender_data = body.get("senderData", {})
        chat_id = sender_data.get("chatId", "")

        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(WhatsAppMessage).where(WhatsAppMessage.message_id == message_id)
            )
            if existing.scalars().first() is not None:
                return
            msg = WhatsAppMessage(
                message_id=message_id,
                chat_id=chat_id,
                sender_id="agent",
                sender_name="RAION",
                direction=direction,
                message_type="text",
                text=body.get("messageData", {}).get("textMessageData", {}).get("textMessage"),
                raw_json=json.dumps(body),
            )
            db.add(msg)
            await db.commit()

        if direction == "outgoing":
            try:
                from app.automations.runtime import on_whatsapp_outgoing
                asyncio.get_running_loop().create_task(
                    on_whatsapp_outgoing(
                        chat_id=chat_id,
                        message_text=body.get("messageData", {}).get("textMessageData", {}).get("textMessage") or "",
                        group_name="",
                    )
                )
            except Exception as _exc:
                logger.warning("whatsapp outgoing automation dispatch failed: %s", _exc)
    except Exception as exc:
        logger.warning("_store_message: failed: %s", exc)


def _detect_message_type(message_data: dict) -> str:
    if "imageMessage" in message_data:
        return "image"
    if "videoMessage" in message_data:
        return "video"
    if "documentMessage" in message_data:
        return "document"
    if "audioMessage" in message_data or "voiceMessage" in message_data:
        return "audio"
    return "text"


def _notify_owner_telegram(text: str) -> None:
    """Fire-and-forget Telegram notification to the owner."""
    import asyncio
    token = app_config.TELEGRAM_BOT_TOKEN
    chat_id = app_config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return

    async def _send() -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
        except Exception as exc:
            logger.warning("_notify_owner_telegram failed: %s", exc)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send())
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Group CRUD API
# ---------------------------------------------------------------------------

class AddGroupBody(BaseModel):
    chat_id: str
    name: str


@router.get("/api/whatsapp/groups")
async def list_groups(_user=Depends(require_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(WhatsAppGroup).order_by(WhatsAppGroup.created_at.desc()))
    groups = result.scalars().all()
    return [
        {
            "id": g.id,
            "chat_id": g.chat_id,
            "name": g.name,
            "description": g.description,
            "enabled": g.enabled,
            "keyword_filter": g.keyword_filter,
            "auto_send_allowed": g.auto_send_allowed,
            "last_message_at": g.last_message_at.isoformat() if g.last_message_at else None,
        }
        for g in groups
    ]


@router.post("/api/whatsapp/groups", status_code=201)
async def add_group(body: AddGroupBody, _user=Depends(require_user), db: AsyncSession = Depends(get_db)):
    existing = await db.execute(
        select(WhatsAppGroup).where(WhatsAppGroup.chat_id == body.chat_id)
    )
    if existing.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="Group already registered")
    group = WhatsAppGroup(chat_id=body.chat_id, name=body.name)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return {"id": group.id, "chat_id": group.chat_id, "name": group.name}


@router.delete("/api/whatsapp/groups/{group_id}", status_code=204)
async def delete_group(group_id: int, _user=Depends(require_user), db: AsyncSession = Depends(get_db)):
    group = await db.get(WhatsAppGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    await db.delete(group)
    await db.commit()


@router.post("/api/whatsapp/groups/{group_id}/toggle")
async def toggle_group(group_id: int, _user=Depends(require_user), db: AsyncSession = Depends(get_db)):
    group = await db.get(WhatsAppGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    group.enabled = not group.enabled
    await db.commit()
    return {"id": group_id, "enabled": group.enabled}


# ---------------------------------------------------------------------------
# Manual send endpoint
# ---------------------------------------------------------------------------

class SendBody(BaseModel):
    chat_id: str
    text: str


@router.post("/api/whatsapp/send")
async def manual_send(body: SendBody, _user=Depends(require_user), db: AsyncSession = Depends(get_db)):
    group_result = await db.execute(
        select(WhatsAppGroup).where(WhatsAppGroup.chat_id == body.chat_id)
    )
    group = group_result.scalars().first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not registered")

    client = get_green_client()
    if client is None:
        raise HTTPException(status_code=503, detail="WhatsApp not configured")

    try:
        await client.send_message(body.chat_id, body.text)
    except GreenAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    msg = WhatsAppMessage(
        message_id=f"manual_{uuid.uuid4().hex}",
        chat_id=body.chat_id,
        sender_id="agent",
        sender_name="RAION",
        direction="outgoing",
        message_type="text",
        text=body.text,
    )
    db.add(msg)
    await db.commit()

    return {"ok": True}


# ---------------------------------------------------------------------------
# Message history
# ---------------------------------------------------------------------------

@router.get("/api/whatsapp/messages/{chat_id:path}")
async def get_messages(
    chat_id: str,
    limit: int = 20,
    _user=Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WhatsAppMessage)
        .where(WhatsAppMessage.chat_id == chat_id)
        .order_by(desc(WhatsAppMessage.created_at))
        .limit(limit)
    )
    msgs = result.scalars().all()
    return [
        {
            "id": m.id,
            "message_id": m.message_id,
            "sender_name": m.sender_name,
            "direction": m.direction,
            "text": m.text,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in msgs
    ]


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------

@router.get("/api/whatsapp/status")
async def whatsapp_status(_user=Depends(require_user)):
    client = get_green_client()
    if client is None:
        return {"stateInstance": "not_configured"}
    try:
        return await client.get_state()
    except GreenAPIError as exc:
        return {"stateInstance": "error", "error": str(exc)}


class WebhookUrlPayload(BaseModel):
    base_url: str


@router.post("/api/whatsapp/webhook-url")
async def set_webhook_url(payload: WebhookUrlPayload, _user=Depends(require_user)):
    """Push a new webhook URL + token to Green API and reboot the instance."""
    client = get_green_client()
    if client is None:
        raise HTTPException(status_code=503, detail="WhatsApp not configured")

    base = payload.base_url.rstrip("/")
    webhook_url = f"{base}/webhook/whatsapp"
    token = app_config.GREEN_API_WEBHOOK_TOKEN

    try:
        await client._post("setSettings", {
            "webhookUrl": webhook_url,
            "webhookUrlToken": token,
            "incomingWebhook": "yes",
            "outgoingWebhook": "yes",
            "outgoingAPIMessageWebhook": "yes",
        })
        await client._get("reboot")
    except GreenAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {"ok": True, "webhook_url": webhook_url}
