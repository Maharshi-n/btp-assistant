from __future__ import annotations

import logging

import openai

import app.config as config
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import AsyncSessionLocal, get_db
from app.db.models import Message, Thread, User
from app.web.deps import require_user
from app.web.routes.ws import manager as ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

AVAILABLE_MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"]

# Module-level singleton — shares the underlying httpx connection pool across requests
_oai_client = openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY)


@router.post("/threads")
async def create_thread(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    thread = Thread(title="New Chat", model="gpt-4o")
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return {
        "id": thread.id,
        "title": thread.title,
        "model": thread.model,
        "created_at": thread.created_at.isoformat(),
    }


@router.get("/threads")
async def list_threads(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(
        select(Thread).order_by(Thread.created_at.desc())
    )
    threads = result.scalars().all()
    return [
        {
            "id": t.id,
            "title": t.title,
            "model": t.model,
            "created_at": t.created_at.isoformat(),
        }
        for t in threads
    ]


@router.get("/threads/{thread_id}/messages")
async def get_messages(
    thread_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    thread = await db.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    result = await db.execute(
        select(Message)
        .where(Message.thread_id == thread_id)
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]


async def _stream_openai(thread_id: int, model: str) -> None:
    """Background task: fetch thread history, stream OpenAI tokens via WebSocket,
    then persist the completed assistant message.  Uses its own DB session so it
    is not tied to the lifetime of the HTTP request session."""
    async with AsyncSessionLocal() as db:
        # Build history from all messages in this thread, oldest first
        result = await db.execute(
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.created_at.asc())
        )
        messages = result.scalars().all()
        history = [{"role": m.role, "content": m.content} for m in messages]

        full_content: list[str] = []

        try:
            stream = await _oai_client.chat.completions.create(
                model=model,
                messages=history,  # type: ignore[arg-type]
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    full_content.append(delta)
                    await ws_manager.send(thread_id, {"type": "token", "content": delta})

        except openai.AuthenticationError:
            logger.warning("OpenAI AuthenticationError for thread %d", thread_id)
            await ws_manager.send(
                thread_id,
                {
                    "type": "error",
                    "content": "Invalid or missing OpenAI API key. Set OPENAI_API_KEY in .env.",
                },
            )
            return
        except openai.APIError as exc:
            logger.error("OpenAI APIError for thread %d: %s", thread_id, exc)
            await ws_manager.send(
                thread_id,
                {"type": "error", "content": f"OpenAI API error: {exc}"},
            )
            return
        except Exception as exc:
            logger.exception("Unexpected error streaming thread %d", thread_id)
            await ws_manager.send(thread_id, {"type": "error", "content": "Internal error. Check server logs."})
            return

        # Persist completed assistant message
        assistant_content = "".join(full_content)
        assistant_msg = Message(
            thread_id=thread_id, role="assistant", content=assistant_content
        )
        db.add(assistant_msg)
        await db.commit()
        await db.refresh(assistant_msg)

        await ws_manager.send(
            thread_id,
            {"type": "done", "message_id": assistant_msg.id},
        )


@router.post("/threads/{thread_id}/messages")
async def post_message(
    thread_id: int,
    payload: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    thread = await db.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    user_content: str = payload.get("content", "").strip()
    if not user_content:
        raise HTTPException(status_code=422, detail="content is required")

    # Persist user message
    user_msg = Message(thread_id=thread_id, role="user", content=user_content)
    db.add(user_msg)
    await db.flush()

    # Update thread title from first message if still default
    if thread.title == "New Chat":
        thread.title = user_content[:60]

    await db.commit()
    await db.refresh(user_msg)

    # Kick off streaming in the background; POST returns immediately
    if ws_manager.active.get(thread_id) is None:
        logger.warning(
            "No WebSocket connected for thread %d — streaming will be lost", thread_id
        )
    background_tasks.add_task(_stream_openai, thread_id, thread.model)

    return {
        "user": {
            "id": user_msg.id,
            "role": user_msg.role,
            "content": user_msg.content,
            "created_at": user_msg.created_at.isoformat(),
        },
    }


@router.patch("/threads/{thread_id}")
async def update_thread(
    thread_id: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    thread = await db.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    if "model" in payload and payload["model"] in AVAILABLE_MODELS:
        thread.model = payload["model"]

    await db.commit()
    return {"id": thread.id, "model": thread.model}
