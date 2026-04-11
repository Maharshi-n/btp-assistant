from __future__ import annotations

import logging

import openai
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agents.supervisor import get_graph
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

# ---------------------------------------------------------------------------
# In-memory store of pending permission requests
# Maps request_id -> {thread_id, tool, args, prompt, lg_config}
# ---------------------------------------------------------------------------
_pending_permissions: dict[str, dict] = {}


def get_pending_permissions() -> dict[str, dict]:
    return _pending_permissions


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


async def _stream_langgraph(
    thread_id: int,
    model: str,
    resume_command: Command | None = None,
) -> None:
    """Background task: stream the LangGraph supervisor and forward events to WebSocket.

    Event types forwarded to the client:
    - {type: "node_start",          node: "supervisor"|"tools"}
    - {type: "node_end",            node: "supervisor"|"tools"}
    - {type: "token",               content: "..."}
    - {type: "tool_call",           tool: "...", args: {...}}
    - {type: "tool_result",         tool: "...", content: "..."}
    - {type: "permission_request",  id, tool, args, prompt}
    - {type: "permission_resolved", id, decision}
    - {type: "done",                message_id: <int>}
    - {type: "error",               content: "..."}
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.created_at.asc())
        )
        messages = result.scalars().all()

        lc_messages = [
            HumanMessage(content=m.content) if m.role == "user"
            else AIMessage(content=m.content)
            for m in messages
        ]

        graph = get_graph()
        lg_config = {
            "configurable": {
                "thread_id": str(thread_id),
                "model": model,
            }
        }

        # Either a fresh invoke with the message history, or a resume after interrupt
        if resume_command is not None:
            graph_input = resume_command
        else:
            graph_input = {"messages": lc_messages}

        full_content: list[str] = []

        try:
            async for event in graph.astream_events(
                graph_input,
                lg_config,
                version="v2",
            ):
                event_type: str = event.get("event", "")
                name: str = event.get("name", "")
                data: dict = event.get("data", {})

                # ── Interrupt (permission request) ─────────────────────────
                if event_type == "on_chain_stream":
                    chunk = data.get("chunk", {})
                    if isinstance(chunk, dict) and "__interrupt__" in chunk:
                        for interrupt_obj in chunk["__interrupt__"]:
                            payload = (
                                interrupt_obj.value
                                if hasattr(interrupt_obj, "value")
                                else interrupt_obj.get("value", {})
                            )
                            if payload.get("type") == "permission_request":
                                request_id = payload["request_id"]
                                # Store so the permissions endpoint can resume
                                _pending_permissions[request_id] = {
                                    "thread_id": thread_id,
                                    "tool": payload["tool"],
                                    "args": payload["args"],
                                    "prompt": payload["prompt"],
                                    "lg_config": lg_config,
                                    "model": model,
                                }
                                await ws_manager.send(thread_id, {
                                    "type": "permission_request",
                                    "id": request_id,
                                    "tool": payload["tool"],
                                    "args": payload["args"],
                                    "prompt": payload["prompt"],
                                })
                        continue  # don't fall through to other handlers

                # ── Node lifecycle ─────────────────────────────────────────
                if event_type == "on_chain_start" and name in ("supervisor", "tools"):
                    await ws_manager.send(
                        thread_id, {"type": "node_start", "node": name}
                    )

                elif event_type == "on_chain_end" and name in ("supervisor", "tools"):
                    await ws_manager.send(
                        thread_id, {"type": "node_end", "node": name}
                    )

                # ── Streamed LLM tokens ────────────────────────────────────
                elif event_type == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk is not None:
                        token: str = chunk.content
                        if token:
                            full_content.append(token)
                            await ws_manager.send(
                                thread_id, {"type": "token", "content": token}
                            )

                # ── Tool calls ─────────────────────────────────────────────
                elif event_type == "on_tool_start":
                    tool_name: str = name
                    tool_args = data.get("input", {})
                    await ws_manager.send(
                        thread_id,
                        {"type": "tool_call", "tool": tool_name, "args": tool_args},
                    )

                # ── Tool results ───────────────────────────────────────────
                elif event_type == "on_tool_end":
                    tool_name = name
                    tool_output = data.get("output", "")
                    if hasattr(tool_output, "content"):
                        tool_output = tool_output.content
                    await ws_manager.send(
                        thread_id,
                        {
                            "type": "tool_result",
                            "tool": tool_name,
                            "content": str(tool_output)[:500],
                        },
                    )

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
                thread_id, {"type": "error", "content": f"OpenAI API error: {exc}"}
            )
            return
        except Exception as exc:
            logger.exception("Unexpected error streaming thread %d", thread_id)
            await ws_manager.send(
                thread_id, {"type": "error", "content": "Internal error. Check server logs."}
            )
            return

        # Persist completed assistant message (only if we got tokens)
        if full_content:
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
        else:
            # Graph paused at interrupt — don't send "done", the UI waits for the
            # permission card to be resolved first.
            pass


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

    user_msg = Message(thread_id=thread_id, role="user", content=user_content)
    db.add(user_msg)
    await db.flush()

    if thread.title == "New Chat":
        thread.title = user_content[:60]

    await db.commit()
    await db.refresh(user_msg)

    if ws_manager.active.get(thread_id) is None:
        logger.warning(
            "No WebSocket connected for thread %d — streaming will be lost", thread_id
        )
    background_tasks.add_task(_stream_langgraph, thread_id, thread.model)

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
