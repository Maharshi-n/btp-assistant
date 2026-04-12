"""Phase 7: Dynamic multi-agent orchestration — supervisor + parallel workers.

Graph topology (supervisor loop):
  START → supervisor_node → (tool calls?)
      ├─ spawn_worker calls   → run_workers_node → supervisor_node (loop)
      ├─ regular tool calls   → policy_tools_node → supervisor_node (loop)
      └─ no tool calls        → END

Worker subgraph (per worker):
  START → worker_node → (tool calls?) → worker_tools_node → worker_node → … → END

RunContext (carried in graph state):
  - recursion_depth   : how many supervisor→spawn layers deep we are (max 3)
  - agent_count       : total agents spawned so far (max 10, counting supervisor)
  - tool_call_count   : total tool calls made across all agents (max 50)
  - start_time        : wall-clock start (max 10 minutes)

Workers stream their own node/tool events back to the WebSocket hub tagged with
their worker_id, so the UI can render a tree panel.

Permission model is unchanged: policy_tools_node applies to both supervisor
and worker tool calls (workers share the same interrupt/resume flow).

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
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, TypedDict

import aiosqlite
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

import app.config as app_config
from app.permissions.policy import get_decision, human_readable_prompt
from app.tools.filesystem import delete_file, list_dir, read_file, write_file
from app.tools.google_tools import GOOGLE_TOOLS
from app.tools.shell import run_shell_command
from app.tools.web import web_fetch, web_search
from app.tools.telegram_tools import telegram_send

# ---------------------------------------------------------------------------
# Bounds (hardcoded per plan)
# ---------------------------------------------------------------------------

MAX_RECURSION_DEPTH = 3
MAX_AGENTS = 10
MAX_TOOL_CALLS = 50
MAX_WALL_CLOCK_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------

class RunContext(TypedDict):
    recursion_depth: int       # current spawn depth (0 = supervisor level)
    agent_count: int           # total agents spawned (supervisor counts as 1)
    tool_call_count: int       # total tool calls across all agents
    start_time: float          # time.monotonic() at the start of the user turn


def _merge_run_context(old: RunContext | None, new: RunContext | None) -> RunContext:
    """Merge run context — new value wins, but never decreases counts.

    Handles the case where `old` is an empty dict `{}` (deserialized from an
    old MessagesState checkpoint that pre-dates Phase 7 — those checkpoints
    have no run_context key, so LangGraph initialises the binop channel with
    the sentinel value it finds, which may be an empty dict rather than None).
    """
    _empty = RunContext(recursion_depth=0, agent_count=1, tool_call_count=0, start_time=time.monotonic())

    # Treat empty dict / falsy as None
    if not old:
        old = None
    if not new:
        new = None

    if old is None and new is None:
        return _empty
    if new is None:
        return old  # type: ignore[return-value]
    if old is None:
        return new
    return RunContext(
        recursion_depth=new.get("recursion_depth", 0),
        agent_count=max(old.get("agent_count", 1), new.get("agent_count", 1)),
        tool_call_count=max(old.get("tool_call_count", 0), new.get("tool_call_count", 0)),
        start_time=old.get("start_time") or new.get("start_time") or time.monotonic(),
    )


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    run_context: Annotated[RunContext, _merge_run_context]


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

WORKER_TOOLS = [
    read_file,
    write_file,
    list_dir,
    delete_file,
    run_shell_command,
    web_search,
    web_fetch,
    *GOOGLE_TOOLS,
    telegram_send,
]

_TOOL_MAP: dict[str, Any] = {t.name: t for t in WORKER_TOOLS}


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

def _supervisor_system_prompt() -> str:
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    date_str = now.strftime("%A, %d %B %Y")
    time_str = now.strftime("%H:%M IST")
    return f"""You are Maharshi's personal AI assistant, running locally on his laptop in India.

Current date and time: {date_str}, {time_str} (IST)
Workspace directory: {app_config.WORKSPACE_DIR}

━━━ WHO YOU ARE ━━━
You are a capable, proactive assistant. You have real tools — use them.
Do not ask for clarification when you can reasonably infer intent and act.
Do not say "I can help with that" — just do it.
When a task is done, give a short, direct summary of what you did.

━━━ TOOLS AVAILABLE ━━━
Filesystem : read_file, write_file, list_dir, delete_file  (workspace-scoped)
Shell      : run_shell_command  (safe allowlist only)
Web        : web_search, web_fetch
Gmail      : gmail_list_unread, gmail_read, gmail_search, gmail_send
Drive      : drive_list, drive_read, drive_write, drive_download, drive_upload
Calendar   : calendar_list_events, calendar_create_event
Telegram   : telegram_send  (automation runs only — notify the user)

━━━ DRIVE RULES ━━━
- To download a file: ALWAYS call drive_list first to get the real file_id. Never guess it.
- drive_download saves the file to workspace. Google Docs→.docx, Sheets→.xlsx, Slides→.pptx.
- drive_upload uploads an EXISTING workspace file. The file must exist before calling this.
  Workflow: write_file (create content) → drive_upload (send to Drive).
- drive_write creates a NEW plain-text file directly on Drive (no local file needed).
- NEVER fabricate content like "<Place your content here>" — if you need to upload something,
  write the actual content with write_file first, then upload with drive_upload.

━━━ TOOL USAGE RULES ━━━
- Always use absolute paths inside {app_config.WORKSPACE_DIR} for file operations.
- For web searches:
    1. Call web_search with max_results=5 first.
    2. READ THE SNIPPETS — DuckDuckGo snippets often contain the answer directly.
       If the answer is in the snippets, use it. Do NOT fetch a URL just to confirm.
    3. Only call web_fetch if the snippets are insufficient AND the URL looks like a
       plain-text/news page (avoid JS-heavy sites like iplt20.com, espncricinfo.com).
    4. Prefer fetching: cricbuzz.com, sports.ndtv.com, bbc.com/sport, timesofindia.com,
       or any URL whose snippet already shows the data you need.
    5. If web_fetch returns empty or garbled content, try a DIFFERENT URL from results
       or search again with a more specific query (e.g. add "scorecard" or "result site:cricbuzz.com").
    6. Never say "I couldn't find it" after only one search — try at least 2 queries.
    7. If the specific thing asked wasn't found, tell the user what WAS found
       (e.g. "GE vs TS not found today, but today's IPL matches were: X vs Y, A vs B").
- For emails: read first with gmail_read, then act. Never fabricate email content.
- For file writes: if writing to a new file, confirm the write succeeded.
- If a tool errors: report the error clearly, try an alternative before giving up.

━━━ TIME-SENSITIVE INFORMATION ━━━
Your training data has a cutoff. For anything about "latest", "current", "today",
"recent news", "prices", "scores", or post-cutoff events — call web_search first.
Cite your sources when reporting current information.

━━━ AUTOMATION RUNS ━━━
When triggered by an automation (cron job, email, file event), the trigger context
is provided at the top of the message. Read it carefully and act on it directly.
For email triggers: the full email is provided — read it, do not call gmail_read again.
For file triggers: the file path is provided — call read_file on that exact path.
For cron triggers: execute the task immediately, do not wait for confirmation.

━━━ TELEGRAM RULES ━━━
- Call telegram_send ONLY when the action_prompt explicitly asks you to notify, alert, or send a notification.
- Keep the message 2-3 sentences, plain text only — no markdown, no bullet points.
- Never call telegram_send more than once per run.
- Only call telegram_send during automation runs — NEVER during regular user chat sessions.
- If telegram_send returns "Telegram not configured", continue normally — do not treat it as an error.

━━━ MULTI-AGENT ORCHESTRATION ━━━
Use spawn_workers ONLY when a task has genuinely independent parallel sub-tasks
(e.g. 3 separate web searches, processing 3 different files simultaneously).
Each worker needs:
  - "task_description": clear, self-contained instruction
  - "tools_allowed": list of tools it may use

Call spawn_workers ONCE with ALL workers. Workers cannot spawn sub-workers.
For simple or sequential tasks, use tools directly — do not over-parallelise."""


def _worker_system_prompt(task_description: str, tools_allowed: list[str]) -> str:
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    tools_str = ', '.join(tools_allowed) if tools_allowed else 'all available tools'
    return f"""You are a focused worker agent. Complete your assigned task and report back.

Task: {task_description}

Date/time : {now.strftime('%A, %d %B %Y, %H:%M IST')}
Workspace : {app_config.WORKSPACE_DIR}
Tools     : {tools_str}

Rules:
- Use absolute paths inside the workspace for all file operations.
- Be decisive — infer intent and act. Do not ask clarifying questions.
- If a tool call fails, try once to fix it, then report the failure clearly.
- When done, give a one-paragraph summary: what you did, what the result was."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_bounds(ctx: RunContext) -> str | None:
    """Return an error string if any bound is exceeded, else None."""
    if ctx["agent_count"] > MAX_AGENTS:
        return f"Agent limit reached ({MAX_AGENTS} agents max). Cannot spawn more workers."
    if ctx["tool_call_count"] >= MAX_TOOL_CALLS:
        return f"Tool call limit reached ({MAX_TOOL_CALLS} calls max). Stopping."
    elapsed = time.monotonic() - ctx["start_time"]
    if elapsed > MAX_WALL_CLOCK_SECONDS:
        return f"Time limit reached ({MAX_WALL_CLOCK_SECONDS}s max). Stopping."
    return None


# ---------------------------------------------------------------------------
# Worker subgraph
# ---------------------------------------------------------------------------

async def _worker_node(state: AgentState, config: RunnableConfig) -> dict:
    """Worker ReAct node — calls LLM, returns tool calls or final answer."""
    cfg = config.get("configurable", {})
    model_name: str = cfg.get("model", "gpt-4o")
    tools_allowed: list[str] = cfg.get("worker_tools_allowed", [])
    worker_id: str = cfg.get("worker_id", "worker")
    thread_id: int = _ws_thread_id(cfg)

    ctx: RunContext = state.get("run_context") or RunContext(
        recursion_depth=0, agent_count=1, tool_call_count=0, start_time=time.monotonic()
    )

    # Bounds check
    err = _check_bounds(ctx)
    if err:
        return {"messages": [AIMessage(content=err)]}

    # Filter tools
    if tools_allowed:
        active_tools = [t for t in WORKER_TOOLS if t.name in tools_allowed]
    else:
        active_tools = WORKER_TOOLS

    llm = ChatOpenAI(
        model=model_name,
        api_key=app_config.OPENAI_API_KEY,
        streaming=True,
    )
    llm_with_tools = llm.bind_tools(active_tools)

    response: BaseMessage = await llm_with_tools.ainvoke(state["messages"])

    # Emit WS event for worker node activity
    try:
        from app.web.routes.ws import manager as ws_manager
        has_tool_calls = bool(getattr(response, "tool_calls", None))
        await ws_manager.send(thread_id, {
            "type": "worker_update",
            "worker_id": worker_id,
            "status": "running" if has_tool_calls else "done",
            "action": "thinking" if has_tool_calls else "completed",
        })
    except Exception:
        pass

    return {"messages": [response]}


def _worker_should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return END


async def _worker_tools_node(state: AgentState, config: RunnableConfig) -> dict:
    """Execute tool calls for a worker agent."""
    last: BaseMessage = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", []) or []

    cfg = config.get("configurable", {})
    worker_id: str = cfg.get("worker_id", "worker")
    thread_id = _ws_thread_id(cfg)
    is_automation = bool(cfg.get("automation_run", False))

    ctx: RunContext = state.get("run_context") or RunContext(
        recursion_depth=0, agent_count=1, tool_call_count=0, start_time=time.monotonic()
    )

    tool_messages: list[ToolMessage] = []

    for tc in tool_calls:
        tool_name: str = tc["name"]
        tool_args: dict = tc["args"] if isinstance(tc["args"], dict) else {}
        tool_call_id: str = tc["id"]

        # Bounds check
        ctx["tool_call_count"] += 1
        err = _check_bounds(ctx)
        if err:
            tool_messages.append(ToolMessage(tool_call_id=tool_call_id, content=err))
            continue

        # Emit tool_call WS event
        try:
            from app.web.routes.ws import manager as ws_manager
            await ws_manager.send(thread_id, {
                "type": "worker_tool_call",
                "worker_id": worker_id,
                "tool": tool_name,
                "args": tool_args,
            })
        except Exception:
            pass

        # Policy check (same as supervisor's tools)
        decision = get_decision(tool_name, tool_args)

        if decision == "ask" and not is_automation:
            request_id = str(uuid.uuid4())
            prompt_text = human_readable_prompt(tool_name, tool_args)

            user_response: dict = interrupt({
                "type": "permission_request",
                "request_id": request_id,
                "tool": tool_name,
                "args": tool_args,
                "prompt": prompt_text,
                "thread_id": thread_id,
            })

            user_decision: str = user_response.get("decision", "denied")

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
            await _log_audit(
                tool_name=tool_name,
                args=tool_args,
                decision="auto",
                decided_by="policy",
                thread_id=thread_id,
                request_id=None,
            )

        # Execute
        t = _TOOL_MAP.get(tool_name)
        if t is None:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Unknown tool: {tool_name}")
            )
            continue

        try:
            if asyncio.iscoroutinefunction(t.func if hasattr(t, "func") else t):
                result = await t.ainvoke(tool_args)
            else:
                result = await asyncio.to_thread(t.invoke, tool_args)

            result_str = str(result)
            tool_messages.append(ToolMessage(tool_call_id=tool_call_id, content=result_str))

            # Emit tool result
            try:
                from app.web.routes.ws import manager as ws_manager
                await ws_manager.send(thread_id, {
                    "type": "worker_tool_result",
                    "worker_id": worker_id,
                    "tool": tool_name,
                    "content": result_str[:300],
                })
            except Exception:
                pass

        except Exception as exc:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Tool error: {exc}")
            )

    return {"messages": tool_messages, "run_context": ctx}


def _build_worker_graph() -> Any:
    """Build the worker ReAct subgraph (not compiled with a checkpointer)."""
    builder: StateGraph = StateGraph(AgentState)
    builder.add_node("worker", _worker_node)
    builder.add_node("tools", _worker_tools_node)
    builder.add_edge(START, "worker")
    builder.add_conditional_edges("worker", _worker_should_continue, ["tools", END])
    builder.add_edge("tools", "worker")
    return builder.compile()


# Module-level worker graph (no checkpointer — workers are ephemeral)
_worker_graph: Any = None


def _get_worker_graph() -> Any:
    global _worker_graph
    if _worker_graph is None:
        _worker_graph = _build_worker_graph()
    return _worker_graph


# ---------------------------------------------------------------------------
# spawn_workers — the tool the supervisor calls
# ---------------------------------------------------------------------------

@tool
def spawn_workers_tool(tasks: list[dict]) -> str:
    """Spawn multiple worker agents in parallel to execute independent tasks.

    Each task dict must have:
    - task_description (str): what the worker should do
    - tools_allowed (list[str]): tool names the worker may use

    This is a placeholder — the actual invocation is handled by
    run_workers_node which intercepts calls to this tool.
    Returns a JSON string of results.
    """
    # This body is never actually executed — run_workers_node intercepts it.
    return json.dumps({"error": "spawn_workers_tool invoked directly — should be intercepted"})


# Full tool list for supervisor (includes spawn_workers)
SUPERVISOR_TOOLS = [
    read_file,
    write_file,
    list_dir,
    delete_file,
    run_shell_command,
    web_search,
    web_fetch,
    *GOOGLE_TOOLS,
    telegram_send,
    spawn_workers_tool,
]

_SUPERVISOR_TOOL_MAP: dict[str, Any] = {t.name: t for t in SUPERVISOR_TOOLS}


# ---------------------------------------------------------------------------
# Supervisor nodes
# ---------------------------------------------------------------------------

def _should_continue(state: AgentState) -> str:
    last: BaseMessage = state["messages"][-1]
    if not (hasattr(last, "tool_calls") and last.tool_calls):
        return END
    # Route: if any call is spawn_workers_tool, go to workers node
    for tc in last.tool_calls:
        if tc["name"] == "spawn_workers_tool":
            return "workers"
    return "tools"


def _ws_thread_id(cfg: dict) -> int:
    """Extract the integer thread_id used for WebSocket routing.

    Automation runs pass a string lg thread_id for checkpointing but store
    the real DB thread_id in ws_thread_id.  Regular chat runs put an integer
    string directly in thread_id.
    """
    if "ws_thread_id" in cfg:
        try:
            return int(cfg["ws_thread_id"])
        except (TypeError, ValueError):
            return 0
    try:
        return int(cfg.get("thread_id", "0"))
    except (TypeError, ValueError):
        return 0


async def supervisor_node(state: AgentState, config: RunnableConfig) -> dict:
    cfg = config.get("configurable", {})
    model_name: str = cfg.get("model", "gpt-4o")
    thread_id: int = _ws_thread_id(cfg)

    # Initialise run context if this is the very first call
    ctx: RunContext = state.get("run_context") or RunContext(
        recursion_depth=0,
        agent_count=1,
        tool_call_count=0,
        start_time=time.monotonic(),
    )

    # Bounds check before calling LLM
    err = _check_bounds(ctx)
    if err:
        # Inject a graceful stop message
        return {
            "messages": [AIMessage(content=f"⚠️ {err}")],
            "run_context": ctx,
        }

    llm = ChatOpenAI(
        model=model_name,
        api_key=app_config.OPENAI_API_KEY,
        streaming=True,
    )
    llm_with_tools = llm.bind_tools(SUPERVISOR_TOOLS)

    messages = list(state["messages"])
    system = SystemMessage(content=_supervisor_system_prompt())
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = system
    else:
        messages.insert(0, system)

    response: BaseMessage = await llm_with_tools.ainvoke(messages)
    return {"messages": [response], "run_context": ctx}


async def policy_tools_node(state: AgentState, config: RunnableConfig) -> dict:
    """Execute regular (non-spawn) tool calls with permission policy."""
    last: BaseMessage = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", []) or []

    cfg = config.get("configurable", {})
    thread_id = _ws_thread_id(cfg)
    is_automation = bool(cfg.get("automation_run", False))

    ctx: RunContext = state.get("run_context") or RunContext(
        recursion_depth=0, agent_count=1, tool_call_count=0, start_time=time.monotonic()
    )

    tool_messages: list[ToolMessage] = []

    for tc in tool_calls:
        tool_name: str = tc["name"]
        tool_args: dict = tc["args"] if isinstance(tc["args"], dict) else {}
        tool_call_id: str = tc["id"]

        # skip spawn_workers_tool here (handled by run_workers_node)
        if tool_name == "spawn_workers_tool":
            continue

        ctx["tool_call_count"] += 1
        err = _check_bounds(ctx)
        if err:
            tool_messages.append(ToolMessage(tool_call_id=tool_call_id, content=err))
            continue

        decision = get_decision(tool_name, tool_args)

        if decision == "ask" and not is_automation:
            request_id = str(uuid.uuid4())
            prompt_text = human_readable_prompt(tool_name, tool_args)

            user_response: dict = interrupt({
                "type": "permission_request",
                "request_id": request_id,
                "tool": tool_name,
                "args": tool_args,
                "prompt": prompt_text,
                "thread_id": thread_id,
            })

            user_decision: str = user_response.get("decision", "denied")

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
            await _log_audit(
                tool_name=tool_name,
                args=tool_args,
                decision="auto",
                decided_by="policy",
                thread_id=thread_id,
                request_id=None,
            )

        t = _SUPERVISOR_TOOL_MAP.get(tool_name)
        if t is None:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Unknown tool: {tool_name}")
            )
            continue

        try:
            if asyncio.iscoroutinefunction(t.func if hasattr(t, "func") else t):
                result = await t.ainvoke(tool_args)
            else:
                result = await asyncio.to_thread(t.invoke, tool_args)
            tool_messages.append(ToolMessage(tool_call_id=tool_call_id, content=str(result)))
        except Exception as exc:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Tool error: {exc}")
            )

    return {"messages": tool_messages, "run_context": ctx}


async def run_workers_node(state: AgentState, config: RunnableConfig) -> dict:
    """Intercept spawn_workers_tool calls and run workers in parallel."""
    last: BaseMessage = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", []) or []

    cfg = config.get("configurable", {})
    thread_id_str: str = cfg.get("thread_id", "0")
    model_name: str = cfg.get("model", "gpt-4o")
    thread_id = _ws_thread_id(cfg)

    ctx: RunContext = state.get("run_context") or RunContext(
        recursion_depth=0, agent_count=1, tool_call_count=0, start_time=time.monotonic()
    )

    tool_messages: list[ToolMessage] = []

    for tc in tool_calls:
        if tc["name"] != "spawn_workers_tool":
            continue

        tool_call_id: str = tc["id"]
        raw_args = tc["args"] if isinstance(tc["args"], dict) else {}
        tasks: list[dict] = raw_args.get("tasks", [])

        if not tasks:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content="No tasks provided to spawn_workers.")
            )
            continue

        # Check recursion depth
        if ctx["recursion_depth"] >= MAX_RECURSION_DEPTH:
            tool_messages.append(
                ToolMessage(
                    tool_call_id=tool_call_id,
                    content=f"Recursion depth limit ({MAX_RECURSION_DEPTH}) reached. Cannot spawn more workers.",
                )
            )
            continue

        # Check agent count before spawning
        new_agent_count = ctx["agent_count"] + len(tasks)
        if new_agent_count > MAX_AGENTS:
            allowed = MAX_AGENTS - ctx["agent_count"]
            if allowed <= 0:
                tool_messages.append(
                    ToolMessage(
                        tool_call_id=tool_call_id,
                        content=f"Agent limit ({MAX_AGENTS}) reached. Cannot spawn any workers.",
                    )
                )
                continue
            tasks = tasks[:allowed]

        ctx["agent_count"] += len(tasks)

        # Notify UI about new workers
        try:
            from app.web.routes.ws import manager as ws_manager
            worker_ids = [str(uuid.uuid4())[:8] for _ in tasks]
            for wid, task in zip(worker_ids, tasks):
                await ws_manager.send(thread_id, {
                    "type": "worker_start",
                    "worker_id": wid,
                    "description": task.get("task_description", "")[:120],
                })
        except Exception:
            worker_ids = [str(uuid.uuid4())[:8] for _ in tasks]

        # Run workers in parallel
        worker_graph = _get_worker_graph()

        async def _run_one(wid: str, task: dict) -> tuple[str, str]:
            """Run one worker and return (worker_id, result_text)."""
            task_description: str = task.get("task_description", "No description")
            tools_allowed: list[str] = task.get("tools_allowed", [])

            worker_config = {
                "configurable": {
                    "thread_id": thread_id_str,
                    "model": model_name,
                    "worker_id": wid,
                    "worker_tools_allowed": tools_allowed,
                }
            }

            init_messages: list[BaseMessage] = [
                SystemMessage(content=_worker_system_prompt(task_description, tools_allowed)),
                HumanMessage(content=task_description),
            ]

            worker_ctx = RunContext(
                recursion_depth=ctx["recursion_depth"] + 1,
                agent_count=ctx["agent_count"],
                tool_call_count=ctx["tool_call_count"],
                start_time=ctx["start_time"],
            )

            init_state: AgentState = {
                "messages": init_messages,
                "run_context": worker_ctx,
            }

            try:
                final_state = await worker_graph.ainvoke(init_state, worker_config)
                messages = final_state.get("messages", [])

                # Update shared tool_call_count from worker's final context
                worker_final_ctx = final_state.get("run_context")
                if worker_final_ctx:
                    ctx["tool_call_count"] = max(ctx["tool_call_count"], worker_final_ctx["tool_call_count"])

                # Extract last AI message as the worker's result
                last_ai = None
                for m in reversed(messages):
                    if isinstance(m, AIMessage):
                        last_ai = m
                        break
                result_text = last_ai.content if last_ai else "Worker completed with no output."

            except Exception as exc:
                result_text = f"Worker error: {exc}"

            # Notify UI that worker is done
            try:
                from app.web.routes.ws import manager as ws_manager
                await ws_manager.send(thread_id, {
                    "type": "worker_end",
                    "worker_id": wid,
                    "status": "done",
                })
            except Exception:
                pass

            return wid, result_text

        results: list[tuple[str, str]] = await asyncio.gather(
            *[_run_one(wid, task) for wid, task in zip(worker_ids, tasks)],
            return_exceptions=False,
        )

        # Aggregate results into the tool message
        parts = []
        for wid, result_text in results:
            task_for_wid = tasks[worker_ids.index(wid)]
            desc = task_for_wid.get("task_description", "")[:80]
            parts.append(f"[Worker {wid}] Task: {desc}\nResult: {result_text}")

        combined = "\n\n---\n\n".join(parts)
        tool_messages.append(ToolMessage(tool_call_id=tool_call_id, content=combined))

    return {"messages": tool_messages, "run_context": ctx}


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
    """Write one row to permission_audit. Best-effort; never raises."""
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
        pass


# ---------------------------------------------------------------------------
# Main supervisor graph builder
# ---------------------------------------------------------------------------

def _build_graph(checkpointer: AsyncSqliteSaver) -> Any:
    builder: StateGraph = StateGraph(AgentState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("tools", policy_tools_node)
    builder.add_node("workers", run_workers_node)

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        _should_continue,
        {"tools": "tools", "workers": "workers", END: END},
    )
    builder.add_edge("tools", "supervisor")
    builder.add_edge("workers", "supervisor")

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
_graph: Any | None = None


def get_graph() -> Any:
    if _graph is None:
        raise RuntimeError("LangGraph supervisor not initialised — did on_startup run?")
    return _graph


async def init_supervisor() -> None:
    global _conn, _checkpointer, _graph, _worker_graph
    db_path = _resolve_db_path()
    _conn = await aiosqlite.connect(db_path)
    _checkpointer = AsyncSqliteSaver(_conn)
    await _checkpointer.setup()
    _graph = _build_graph(_checkpointer)
    # Pre-build the worker graph too
    _worker_graph = _build_worker_graph()


async def shutdown_supervisor() -> None:
    global _conn, _checkpointer, _graph, _worker_graph
    _graph = None
    _checkpointer = None
    _worker_graph = None
    if _conn is not None:
        try:
            await _conn.close()
        except Exception:
            pass
        _conn = None
