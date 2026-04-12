"""Telegram webhook — receives incoming messages and resumes pending automations."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import delete, select

import app.config as app_config
from app.db.engine import AsyncSessionLocal
from app.db.models import Message, TelegramPendingReply, Thread

logger = logging.getLogger(__name__)

router = APIRouter()


async def _run_continuation(continuation_prompt: str, user_reply: str) -> str:
    """Create a thread and run the supervisor with the continuation prompt + user reply.

    Returns the final assistant content string.
    """
    full_prompt = (
        "[AUTOMATION RUN — execute immediately, no questions, no clarifications. "
        "Call tools directly as instructed. Do not ask the user anything.]\n\n"
        + continuation_prompt
        + f"\n\nUser's reply: {user_reply}"
    )

    async with AsyncSessionLocal() as db:
        thread = Thread(title="[Telegram Reply]", model="gpt-4o")
        db.add(thread)
        await db.flush()

        user_msg = Message(
            thread_id=thread.id,
            role="user",
            content=full_prompt,
            metadata_json=json.dumps({"telegram_reply": True, "automation_run": True}),
        )
        db.add(user_msg)
        await db.commit()
        await db.refresh(thread)
        thread_id = thread.id

    lg_thread_id = f"tg_reply_{uuid.uuid4().hex[:12]}"

    try:
        from langchain_core.messages import AIMessage, HumanMessage
        from app.agents.supervisor import get_graph

        graph = get_graph()
        lg_config = {
            "configurable": {
                "thread_id": lg_thread_id,
                "ws_thread_id": thread_id,
                "model": "gpt-4o",
                "automation_run": True,
            }
        }

        lc_messages = [HumanMessage(content=full_prompt)]
        full_content: list[str] = []
        last_ai_content: str = ""

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

        # Persist assistant reply
        if final_content:
            async with AsyncSessionLocal() as db2:
                msg = Message(
                    thread_id=thread_id,
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
    # Validate secret token
    expected_secret = app_config.TELEGRAM_WEBHOOK_SECRET
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        # Always return 200 to Telegram to prevent retries
        return {"ok": True}

    # Extract chat_id and text from Telegram update
    message = body.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return {"ok": True}

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
            # No pending reply — silently ignore
            return {"ok": True}

        continuation_prompt = pending.continuation_prompt
        # Delete the pending reply (consumed)
        await db.execute(
            delete(TelegramPendingReply).where(TelegramPendingReply.chat_id == chat_id)
        )
        await db.commit()

    # Send immediate acknowledgement
    token = app_config.TELEGRAM_BOT_TOKEN
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

    # Run the supervisor in the background so we return 200 immediately
    async def _run_and_notify() -> None:
        result = await _run_continuation(continuation_prompt, text)
        # Send result summary back to Telegram
        if token and chat_id:
            try:
                import httpx
                # Truncate very long results for Telegram (4096 char limit)
                summary = result[:1000] + ("..." if len(result) > 1000 else "")
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": f"✓ Done.\n\n{summary}"},
                    )
            except Exception as exc:
                logger.warning("telegram webhook: failed to send result: %s", exc)

    asyncio.create_task(_run_and_notify())

    return {"ok": True}
