"""Phase 6: LangGraph ReAct supervisor with permission-gated tools.

Graph topology:
  START → supervisor_node → (if tool calls) policy_tools_node → supervisor_node → …
                          → (if no tool calls) END

The policy_tools_node replaces the plain ToolNode from Phase 5.  For each
pending tool call it:
  1. Consults app/permissions/policy.py.
  2. If "auto" — executes the tool directly.
  3. If "ask" — calls LangGraph interrupt() with a structured payload.
     The graph pauses and checkpoints.  The server pushes a WebSocket
     permission_request event to the client.
     When the user approves/denies, the server calls:
         graph.ainvoke(Command(resume={"decision": "approved"|"denied", ...}), config)
     and execution resumes from just after the interrupt() call.
  4. If denied — injects a ToolMessage saying the action was denied.

Every auto-approved and user-decided permission is logged to the
permission_audit table.

Callers pass model + thread_id in the LangGraph invocation config:

    config = {
        "configurable": {
            "thread_id": str(thread_id),
            "model": "gpt-4o",
        }
    }
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt

import app.config as app_config
from app.permissions.policy import get_decision, human_readable_prompt
from app.tools.filesystem import delete_file, list_dir, read_file, write_file
from app.tools.shell import run_shell_command
from app.tools.web import web_fetch, web_search

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    read_file,
    write_file,
    list_dir,
    delete_file,
    run_shell_command,
    web_search,
    web_fetch,
]

_TOOL_MAP: dict[str, Any] = {t.name: t for t in ALL_TOOLS}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _system_prompt() -> str:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %d %B %Y")
    time_str = now.strftime("%H:%M UTC")
    return f"""You are a personal AI assistant running on Maharshi's laptop.

Today's date and time: {date_str}, {time_str}

IMPORTANT — handling time-sensitive queries:
- Your training data has a cutoff. For anything involving "latest", "current",
  "today", "recent", "news", "prices", "scores", or events after your cutoff,
  you MUST call web_search first and base your answer on those results.
- Never state a current date, time, or recent fact from memory alone — always
  confirm with a search if there is any doubt.
- When you do a web search for news or current events, tell the user which
  sources your answer is based on.

Workspace directory: {app_config.WORKSPACE_DIR}
You can read, write, list, and delete files inside the workspace.
Shell commands are restricted to a safe allowlist."""


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def _should_continue(state: MessagesState) -> str:
    last: BaseMessage = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return END


async def supervisor_node(state: MessagesState, config: RunnableConfig) -> dict:
    model_name: str = config.get("configurable", {}).get("model", "gpt-4o")
    llm = ChatOpenAI(
        model=model_name,
        api_key=app_config.OPENAI_API_KEY,
        streaming=True,
    )
    llm_with_tools = llm.bind_tools(ALL_TOOLS)

    messages = list(state["messages"])
    system = SystemMessage(content=_system_prompt())
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = system
    else:
        messages.insert(0, system)

    response: BaseMessage = await llm_with_tools.ainvoke(messages)
    return {"messages": [response]}


async def policy_tools_node(state: MessagesState, config: RunnableConfig) -> dict:
    """Execute tool calls, pausing via interrupt() for any that need approval.

    This node processes all tool calls in the last AI message.  For each one:
      - Consults policy → "auto" or "ask"
      - "auto": runs the tool, logs to permission_audit
      - "ask":  calls interrupt() to pause the graph; resumes with user decision
    """
    last: BaseMessage = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", []) or []

    thread_id_str: str = config.get("configurable", {}).get("thread_id", "0")
    try:
        thread_id = int(thread_id_str)
    except ValueError:
        thread_id = 0

    tool_messages: list[ToolMessage] = []

    for tc in tool_calls:
        tool_name: str = tc["name"]
        tool_args: dict = tc["args"] if isinstance(tc["args"], dict) else {}
        tool_call_id: str = tc["id"]

        decision = get_decision(tool_name, tool_args)

        if decision == "ask":
            request_id = str(uuid.uuid4())
            prompt_text = human_readable_prompt(tool_name, tool_args)

            # Pause the graph here — the server will push a WS event and wait
            # for the user to POST /api/permissions/<request_id>.
            # The resume value is {"decision": "approved"|"denied", "request_id": ...}
            user_response: dict = interrupt({
                "type": "permission_request",
                "request_id": request_id,
                "tool": tool_name,
                "args": tool_args,
                "prompt": prompt_text,
                "thread_id": thread_id,
            })

            user_decision: str = user_response.get("decision", "denied")

            # Log to DB (best-effort — don't crash the graph if DB is unavailable)
            await _log_audit(
                tool_name=tool_name,
                args=tool_args,
                decision="approved" if user_decision == "approved" else "denied",
                decided_by="user",
                thread_id=thread_id,
                request_id=request_id,
            )

            if user_decision != "approved":
                tool_messages.append(
                    ToolMessage(
                        tool_call_id=tool_call_id,
                        content=f"Action denied by user: {prompt_text}",
                    )
                )
                continue

        else:
            # Auto — log it
            await _log_audit(
                tool_name=tool_name,
                args=tool_args,
                decision="auto",
                decided_by="policy",
                thread_id=thread_id,
                request_id=None,
            )

        # Execute the tool
        tool = _TOOL_MAP.get(tool_name)
        if tool is None:
            tool_messages.append(
                ToolMessage(
                    tool_call_id=tool_call_id,
                    content=f"Error: unknown tool '{tool_name}'.",
                )
            )
            continue

        try:
            # LangChain tools may be sync or async; handle both
            if asyncio.iscoroutinefunction(tool.func if hasattr(tool, "func") else tool):
                result = await tool.ainvoke(tool_args)
            else:
                result = await asyncio.to_thread(tool.invoke, tool_args)
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=str(result))
            )
        except Exception as exc:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Tool error: {exc}")
            )

    return {"messages": tool_messages}


# ---------------------------------------------------------------------------
# DB audit helper
# ---------------------------------------------------------------------------

async def _log_audit(
    *,
    tool_name: str,
    args: dict,
    decision: str,
    decided_by: str,
    thread_id: int,
    request_id: str | None,
) -> None:
    """Write one row to permission_audit.  Best-effort; never raises."""
    try:
        from app.db.engine import AsyncSessionLocal
        from app.db.models import PermissionAudit

        async with AsyncSessionLocal() as db:
            row = PermissionAudit(
                tool_name=tool_name,
                args_json=json.dumps(args, default=str),
                decision=decision,
                decided_by=decided_by,
                decided_at=datetime.now(timezone.utc),
                thread_id=thread_id,
                request_id=request_id,
            )
            db.add(row)
            await db.commit()
    except Exception:
        pass  # never crash the graph over audit logging


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _build_graph(checkpointer: AsyncSqliteSaver) -> object:
    builder: StateGraph = StateGraph(MessagesState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("tools", policy_tools_node)

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", _should_continue, ["tools", END])
    builder.add_edge("tools", "supervisor")

    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# DB path helper
# ---------------------------------------------------------------------------

def _resolve_db_path() -> str:
    url = app_config.DATABASE_URL
    if ":///" in url:
        return url.split("///", 1)[-1]
    return "app.db"


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_conn: aiosqlite.Connection | None = None
_checkpointer: AsyncSqliteSaver | None = None
_graph: object | None = None


def get_graph() -> object:
    if _graph is None:
        raise RuntimeError("LangGraph supervisor not initialised — did on_startup run?")
    return _graph


async def init_supervisor() -> None:
    global _conn, _checkpointer, _graph
    db_path = _resolve_db_path()
    _conn = await aiosqlite.connect(db_path)
    _checkpointer = AsyncSqliteSaver(_conn)
    await _checkpointer.setup()
    _graph = _build_graph(_checkpointer)


async def shutdown_supervisor() -> None:
    global _conn, _checkpointer, _graph
    _graph = None
    _checkpointer = None
    if _conn is not None:
        try:
            await _conn.close()
        except Exception:
            pass
        _conn = None
