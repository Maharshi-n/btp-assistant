"""WhatsApp integration routes — webhook, group CRUD, manual send, management UI."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
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
from app.db.models import Message, Thread, WhatsAppGroup, WhatsAppMessage, WhatsAppPendingThread
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

    # Green API uses two layouts:
    # 1. Old: messageData.imageMessage / videoMessage / documentMessage / locationMessage
    # 2. New: messageData.typeMessage = "imageMessage" + messageData.fileMessageData / locationMessageData
    _type_msg = message_data.get("typeMessage", "")
    _file_data = message_data.get("fileMessageData") or {}

    _img = (message_data.get("imageMessage") or message_data.get("imageMessageData")
            or (_file_data if _type_msg == "imageMessage" else {}))
    _vid = (message_data.get("videoMessage") or message_data.get("videoMessageData")
            or (_file_data if _type_msg == "videoMessage" else {}))
    _doc = (message_data.get("documentMessage") or message_data.get("documentMessageData")
            or (_file_data if _type_msg == "documentMessage" else {}))
    _aud = (message_data.get("audioMessage") or message_data.get("voiceMessage")
            or message_data.get("audioMessageData") or message_data.get("voiceMessageData")
            or (_file_data if _type_msg in ("audioMessage", "voiceMessage") else {}))
    _loc = (message_data.get("locationMessage") or message_data.get("locationMessageData")
            or (message_data.get("locationMessageData") if _type_msg == "locationMessage" else {}))
    _live = (message_data.get("liveLocationMessage") or message_data.get("liveLocationMessageData")
             or (message_data.get("liveLocationMessageData") if _type_msg == "liveLocationMessage" else {}))

    text: str = (
        message_data.get("textMessageData", {}).get("textMessage", "")
        or message_data.get("extendedTextMessageData", {}).get("text", "")
        or _img.get("caption", "")
        or _vid.get("caption", "")
        or _doc.get("caption", "")
        or ""
    )
    media_url: str = (
        _img.get("downloadUrl", "")
        or _vid.get("downloadUrl", "")
        or _doc.get("downloadUrl", "")
        or ""
    )

    # Enrich text with structured markers for non-text message types so automations
    # can act on them semantically without needing to parse raw JSON.
    if _loc:
        lat = _loc.get("latitude", "")
        lng = _loc.get("longitude", "")
        loc_name = _loc.get("nameLocation", "") or _loc.get("address", "")
        text = f"[LOCATION SHARED] {loc_name} lat={lat} lng={lng}".strip()
    elif _live:
        lat = _live.get("latitude", "")
        lng = _live.get("longitude", "")
        text = f"[LIVE LOCATION SHARED] lat={lat} lng={lng}"
    elif not text and _img:
        text = "[IMAGE SENT] (no caption)"
    elif not text and _vid:
        text = "[VIDEO SENT] (no caption)"
    elif not text and _aud:
        text = "[AUDIO/VOICE MESSAGE SENT]"
    elif not text and _doc:
        fname = _doc.get("fileName", "")
        text = f"[DOCUMENT SENT] {fname}".strip()

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

    # Interactive mode check
    if group and group.enabled and group.interactive_mode:
        asyncio.get_running_loop().create_task(
            _handle_interactive_message(chat_id, sender_name, text, group)
        )
        return

    # Automation trigger (fire-and-forget, unchanged)
    try:
        from app.automations.runtime import on_whatsapp_message
        asyncio.get_running_loop().create_task(
            on_whatsapp_message(
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name=sender_name,
                message_text=text,
                group_name=group.name if group else "",
                message_type=_detect_message_type(message_data),
                media_url=media_url or "",
                message_id=message_id,
            )
        )
    except Exception as exc:
        logger.warning("WhatsApp automation dispatch failed: %s", exc)


async def _handle_interactive_message(
    chat_id: str,
    sender_name: str,
    text: str,
    group: WhatsAppGroup,
) -> None:
    """Handle a message from an interactive-mode group."""
    _wa_cancel_idle_close(chat_id)

    # Bye/exit → close thread
    if _wa_is_end_reply(text):
        await _wa_close_thread(chat_id, "Talk to you later! 👋")
        return

    # Look up existing open thread
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
        )
        pending = result.scalars().first()

    now = datetime.now(timezone.utc)

    if pending and pending.expires_at.replace(tzinfo=timezone.utc) > now:
        # Continue existing thread
        thread_id = pending.thread_id
    else:
        # Open new thread
        async with AsyncSessionLocal() as db:
            thread = Thread(
                title=f"WhatsApp: {group.name} — {text[:50]}",
                model=app_config.DEFAULT_THREAD_MODEL,
            )
            db.add(thread)
            await db.commit()
            await db.refresh(thread)
            thread_id = thread.id

    await _run_wa_interactive(chat_id, text, thread_id, sender_name)


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
    t = message_data.get("typeMessage", "")
    if t in ("locationMessage", "liveLocationMessage") or any(
        k in message_data for k in ("locationMessage", "liveLocationMessage", "locationMessageData", "liveLocationMessageData")
    ):
        return "location"
    if t == "imageMessage" or any(k in message_data for k in ("imageMessage", "imageMessageData")):
        return "image"
    if t == "videoMessage" or any(k in message_data for k in ("videoMessage", "videoMessageData")):
        return "video"
    if t == "documentMessage" or any(k in message_data for k in ("documentMessage", "documentMessageData")):
        return "document"
    if t in ("audioMessage", "voiceMessage") or any(
        k in message_data for k in ("audioMessage", "voiceMessage", "audioMessageData", "voiceMessageData")
    ):
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
# Interactive mode — idle close + bye/exit detection
# ---------------------------------------------------------------------------

_WA_IDLE_TIMEOUT_SECONDS = 120
_WA_END_PHRASES = {
    "no", "nope", "nah", "nothing", "nothing else", "that's all", "thats all",
    "that's it", "thats it", "done", "bye", "goodbye", "thanks", "thank you",
    "ok thanks", "ok thank you", "no thanks", "no thank you", "all good",
    "i'm good", "im good", "stop", "exit", "quit", "end",
}


def _wa_is_end_reply(text: str) -> bool:
    return text.lower().strip().rstrip("!.") in _WA_END_PHRASES


def _wa_idle_job_id(chat_id: str) -> str:
    return f"wa_idle_{chat_id}"


def _wa_schedule_idle_close(chat_id: str, thread_id: int) -> None:
    """Schedule (or reschedule) a 2-min idle-close job for this WhatsApp chat."""
    try:
        from app.automations.runtime import get_scheduler
        from apscheduler.triggers.date import DateTrigger

        scheduler = get_scheduler()
        if scheduler is None or not scheduler.running:
            return
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=_WA_IDLE_TIMEOUT_SECONDS)
        scheduler.add_job(
            _wa_fire_idle_close,
            trigger=DateTrigger(run_date=fire_at),
            id=_wa_idle_job_id(chat_id),
            args=[chat_id, thread_id],
            replace_existing=True,
            max_instances=1,
        )
    except Exception as exc:
        logger.warning("_wa_schedule_idle_close: failed: %s", exc)


def _wa_cancel_idle_close(chat_id: str) -> None:
    try:
        from app.automations.runtime import get_scheduler
        scheduler = get_scheduler()
        if scheduler is None or not scheduler.running:
            return
        job = scheduler.get_job(_wa_idle_job_id(chat_id))
        if job:
            job.remove()
    except Exception as exc:
        logger.warning("_wa_cancel_idle_close: failed: %s", exc)


async def _wa_fire_idle_close(chat_id: str, thread_id: int) -> None:
    """APScheduler job: clear pending thread and notify user via WhatsApp."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
            )
            pending = result.scalars().first()
            if pending is None or pending.thread_id != thread_id:
                return
            if pending.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                return
            await db.execute(
                delete(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
            )
            await db.commit()
    except Exception as exc:
        logger.warning("_wa_fire_idle_close: db error: %s", exc)
        return

    try:
        from app.integrations.green_api import get_green_client
        client = get_green_client()
        if client:
            await client.send_message(chat_id, "Thread closed due to inactivity. Send a message anytime to start a new one.")
    except Exception as exc:
        logger.warning("_wa_fire_idle_close: send failed: %s", exc)

    logger.info("_wa_fire_idle_close: closed idle thread #%s for chat %s", thread_id, chat_id)


async def _wa_register_pending_thread(chat_id: str, thread_id: int) -> None:
    """Upsert a WhatsAppPendingThread row and (re)schedule idle close."""
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
        )
        db.add(WhatsAppPendingThread(chat_id=chat_id, thread_id=thread_id, expires_at=expires))
        await db.commit()
    _wa_schedule_idle_close(chat_id, thread_id)


async def _wa_close_thread(chat_id: str, farewell: str = "Talk to you later! 👋") -> None:
    """Close the pending thread and send farewell message."""
    _wa_cancel_idle_close(chat_id)
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(WhatsAppPendingThread).where(WhatsAppPendingThread.chat_id == chat_id)
        )
        await db.commit()
    try:
        from app.integrations.green_api import get_green_client
        client = get_green_client()
        if client:
            await client.send_message(chat_id, farewell)
    except Exception as exc:
        logger.warning("_wa_close_thread: send failed: %s", exc)


async def _run_wa_interactive(chat_id: str, text: str, thread_id: int, sender_name: str) -> None:
    """Run the agent on an interactive-mode WhatsApp message and reply."""
    from langchain_core.messages import AIMessage, HumanMessage
    from app.agents.supervisor import get_graph

    db_thread_id = thread_id
    lg_thread_id = f"wa_{db_thread_id}"
    tagged_reply = f"[via WhatsApp interactive] [sender: {sender_name}] {text}"

    async with AsyncSessionLocal() as db:
        thread = await db.get(Thread, db_thread_id)
        model = thread.model if thread else app_config.DEFAULT_THREAD_MODEL
        msg = Message(
            thread_id=db_thread_id,
            role="user",
            content=text,
            metadata_json=json.dumps({"whatsapp_reply": True}),
        )
        db.add(msg)
        await db.commit()

    graph = get_graph()
    lg_config = {
        "recursion_limit": 100,
        "configurable": {
            "thread_id": lg_thread_id,
            "ws_thread_id": db_thread_id,
            "model": model,
            "automation_run": True,
        },
    }

    lc_messages: list = [HumanMessage(content=tagged_reply)]
    full_content: list[str] = []
    last_ai_content: str = ""

    try:
        async for event in graph.astream_events({"messages": lc_messages}, lg_config, version="v2"):
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
                ai_msg = Message(
                    thread_id=db_thread_id,
                    role="assistant",
                    content=final_content,
                    metadata_json=json.dumps({"whatsapp_reply": True}),
                )
                db2.add(ai_msg)
                await db2.commit()

        result_text = final_content or "Done."

    except Exception as exc:
        logger.exception("_run_wa_interactive: supervisor run failed: %s", exc)
        try:
            checkpointer = get_graph().checkpointer
            if checkpointer is not None:
                await checkpointer.adelete_thread(lg_thread_id)
        except Exception as ce:
            logger.warning("_run_wa_interactive: failed to clear checkpoint: %s", ce)
        exc_str = str(exc)
        if "Recursion limit" in exc_str or "GraphRecursion" in type(exc).__name__:
            result_text = "I got stuck in a retry loop and had to stop. Try again in a moment."
        else:
            result_text = f"Something went wrong: {exc_str[:300]}"

    if result_text:
        try:
            from app.integrations.green_api import get_green_client
            client = get_green_client()
            if client:
                for chunk in _split_message(result_text, 4000):
                    await client.send_message(chat_id, chunk)
        except Exception as exc:
            logger.warning("_run_wa_interactive: send failed: %s", exc)

    await _wa_register_pending_thread(chat_id, thread_id)


def _split_message(text: str, max_len: int) -> list[str]:
    """Split long text into chunks at newline boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


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
            "interactive_mode": g.interactive_mode,
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


@router.post("/api/whatsapp/groups/{group_id}/toggle-interactive")
async def toggle_interactive_mode(group_id: int, _user=Depends(require_user), db: AsyncSession = Depends(get_db)):
    group = await db.get(WhatsAppGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    group.interactive_mode = not group.interactive_mode
    await db.commit()
    return {"id": group_id, "interactive_mode": group.interactive_mode}


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


# ---------------------------------------------------------------------------
# Polling toggle
# ---------------------------------------------------------------------------

@router.get("/api/whatsapp/polling")
async def get_polling_status(_user=Depends(require_user)):
    from app.automations.runtime import get_wa_polling_enabled
    return {"enabled": get_wa_polling_enabled()}


@router.post("/api/whatsapp/polling/toggle")
async def toggle_polling(_user=Depends(require_user)):
    from app.automations.runtime import get_wa_polling_enabled, set_wa_polling_enabled
    new_state = not get_wa_polling_enabled()
    set_wa_polling_enabled(new_state)
    return {"enabled": new_state}
