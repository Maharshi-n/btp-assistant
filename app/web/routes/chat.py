from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import Message, Thread, User
from app.web.deps import require_user

router = APIRouter(prefix="/api")

AVAILABLE_MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"]


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


@router.post("/threads/{thread_id}/messages")
async def post_message(
    thread_id: int,
    payload: dict,
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

    # Stub echo reply
    assistant_content = f"Echo: {user_content}"
    assistant_msg = Message(thread_id=thread_id, role="assistant", content=assistant_content)
    db.add(assistant_msg)

    # Update thread title from first message if still default
    if thread.title == "New Chat":
        thread.title = user_content[:60]

    await db.commit()
    await db.refresh(user_msg)
    await db.refresh(assistant_msg)

    return {
        "user": {
            "id": user_msg.id,
            "role": user_msg.role,
            "content": user_msg.content,
            "created_at": user_msg.created_at.isoformat(),
        },
        "assistant": {
            "id": assistant_msg.id,
            "role": assistant_msg.role,
            "content": assistant_msg.content,
            "created_at": assistant_msg.created_at.isoformat(),
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
