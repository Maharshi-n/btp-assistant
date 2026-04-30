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
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, TypedDict
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

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
from app.tools.filesystem import clear_file, copy_file, create_folder, delete_file, find_file, list_dir, move_file, read_file, write_file
from app.tools.google_tools import GOOGLE_TOOLS
from app.tools.shell import run_shell_command
from app.tools.web import web_fetch, web_search
from app.tools.telegram_tools import save_draft, schedule_message, telegram_ask, telegram_send, telegram_send_file
from app.tools.whatsapp_tools import (
    whatsapp_fetch_messages,
    whatsapp_get_groups,
    whatsapp_read_messages,
    whatsapp_send,
    whatsapp_send_file,
)
from app.tools.image import generate_image
from app.tools.skills import read_skill
from app.tools.rag import rag_ingest, rag_search
from app.tools.database import query_database
from app.tools.python_runner import run_python
from app.mcp.loader import load_active_mcp_tools

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
    clear_file,
    copy_file,
    move_file,
    create_folder,
    find_file,
    list_dir,
    delete_file,
    run_shell_command,
    web_search,
    web_fetch,
    *GOOGLE_TOOLS,
    telegram_send,
    telegram_ask,
    save_draft,
    schedule_message,
    telegram_send_file,
    whatsapp_send,
    whatsapp_send_file,
    whatsapp_read_messages,
    whatsapp_fetch_messages,
    whatsapp_get_groups,
    read_skill,
    rag_ingest,
    rag_search,
    query_database,
    run_python,
]

_TOOL_MAP: dict[str, Any] = {t.name: t for t in WORKER_TOOLS}


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_memory_cache: dict[str, Any] = {"text": "", "loaded_at": 0.0, "ttl": 10.0}
_skills_cache: dict[str, Any] = {"text": "", "loaded_at": 0.0, "ttl": 10.0}


def invalidate_memory_cache() -> None:
    """Force the next _load_user_memories() call to re-read from DB."""
    _memory_cache["loaded_at"] = 0.0


def invalidate_skills_cache() -> None:
    """Force the next _load_skills_index() call to re-read from DB."""
    _skills_cache["loaded_at"] = 0.0


async def _load_skills_index() -> str:
    """Return a compact index of enabled skills for injection into the system prompt.

    Only names + trigger descriptions are included here (1 line each).
    The full file content is loaded on demand via read_skill().
    10-second TTL cache — same pattern as memories.
    """
    now = time.monotonic()
    if now - _skills_cache["loaded_at"] < _skills_cache["ttl"]:
        return _skills_cache["text"]

    try:
        from sqlalchemy import select as sa_select
        from app.db.engine import AsyncSessionLocal
        from app.db.models import Skill

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sa_select(Skill)
                .where(Skill.enabled == True)  # noqa: E712
                .order_by(Skill.name.asc())
            )
            skills = result.scalars().all()

        if not skills:
            text = ""
        else:
            lines = "\n".join(f'- "{s.name}": {s.trigger_description}' for s in skills)
            text = (
                "\n\n━━━ SKILLS ━━━\n"
                "You have access to the following skill files via read_skill(name).\n"
                "Call read_skill() with the exact name when a skill is relevant:\n"
                + lines
            )

        _skills_cache["text"] = text
        _skills_cache["loaded_at"] = now
        return text
    except Exception:
        return _skills_cache.get("text", "")


async def _load_user_memories() -> str:
    """Fetch all user memory entries from the DB and format them for injection.

    Results are cached for 10 seconds so repeated supervisor loop iterations
    don't hit the DB on every LLM call.
    """
    now = time.monotonic()
    if now - _memory_cache["loaded_at"] < _memory_cache["ttl"]:
        return _memory_cache["text"]

    try:
        from sqlalchemy import select as sa_select
        from app.db.engine import AsyncSessionLocal
        from app.db.models import UserMemory

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sa_select(UserMemory).order_by(UserMemory.created_at.asc())
            )
            memories = result.scalars().all()

        if not memories:
            text = ""
        else:
            lines = "\n".join(f"- {m.content}" for m in memories)
            text = f"\n\n━━━ USER MEMORY ━━━\nThe user has stored the following personal context. Use it to personalize your responses:\n{lines}"

        _memory_cache["text"] = text
        _memory_cache["loaded_at"] = now
        return text
    except Exception:
        return _memory_cache.get("text", "")


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
Filesystem : read_file, write_file, clear_file, copy_file, move_file, create_folder, find_file, list_dir, delete_file  (workspace-scoped)
           copy_file/move_file preserve binary content — use these for images, PDFs, and any non-text files
Shell      : run_shell_command  (any command — ask before destructive actions like rm, drop db, force push, kill)
           For installing software: try winget first: winget install <AppName>
           If winget fails, use browser (see Playwright below).
Browser    : mcp__playwright__browser_navigate/screenshot/click/type/snapshot/scroll/close
           SCREENSHOTS: after browser_take_screenshot the system auto-saves the PNG to
           D:\\screenshots\\<filename>.png — the returned tool result contains the exact
           path. Use that returned path verbatim in telegram_send_file. Do NOT call
           copy_file. Do NOT invent paths starting with '@'.
           Always read_skill("mcp_playwright") before any browser task.
Web        : web_search, web_fetch
Gmail      : gmail_list_unread, gmail_read, gmail_search, gmail_send
Drive      : drive_list, drive_read, drive_write, drive_download, drive_upload
Calendar   : calendar_list_events, calendar_create_event
Telegram   : telegram_send, telegram_ask, save_draft, schedule_message, telegram_send_file
WhatsApp   : whatsapp_get_groups (list groups+chat_ids), whatsapp_send (text), whatsapp_send_file (local file), whatsapp_read_messages (live API history), whatsapp_fetch_messages (DB query by time window — use for summaries, reports, "today's messages", automations)
Images     : generate_image  (DALL-E 3, saves to workspace/images/, $0.04/image)
Skills     : read_skill  (call when a skill from the SKILLS section is relevant)
Databases  : query_database(connection_id, sql)  — run SELECT queries against connected DBs
Python     : run_python(code)  — execute Python/pandas scripts for file merging, filtering, transforming
             writes script to workspace/tmp/, 60s timeout, returns stdout + new files created
             use for ANY task that involves combining/processing files — do NOT do this via LLM
RAG        : rag_ingest, rag_search  (vector search over local files)

━━━ RAG RULES ━━━
Use rag_ingest + rag_search when:
- Finding/locating something across multiple files ("which file mentions X", "does any file talk about Y")
- Answering a specific question from a large file (don't need full content, just the relevant part)
- Semantic search across files ("find content related to neural networks")
- Cross-file topic comparison ("how do these files differ on topic X")

Use read_file directly when:
- Full extraction needed ("give me all questions/headings from this file")
- Summarizing an entire file (needs full content)
- File is small (under ~5KB) — cheaper and more accurate to read directly
- Structured data files (CSV, JSON) — use csv_analyze skill or read directly
- Code files where full context matters

Workflow: rag_ingest(paths) first to ensure files are indexed, then rag_search(query, paths).

━━━ PYTHON DATA RULES ━━━
For ANY task involving filtering, merging, aggregating, or transforming files (Excel, CSV):
1. NEVER do it by reading the file content into your context — data will be wrong or truncated.
2. ALWAYS use run_python with a pandas script.
3. ALWAYS call read_skill("python_data_ops") first to get the correct patterns.
4. The script MUST print row count and output file path at the end.
5. If run_python returns Exit: 1 — read stderr, fix the code, call run_python again.
6. NEVER claim a file was created without seeing it in the "Files created:" section of run_python output.
7. Workspace path convention: use 'workspace/filename.xlsx' (relative to project root).

━━━ DATABASE RULES ━━━
For ANY question about data in a connected database — counts, records, queries, tables:
1. DO NOT ask the user for connection names or table names. You have everything you need.
2. Look at the SKILLS section below — any skill starting with "db_" is a database. Use it.
3. Call read_skill("<skill_name>") immediately (e.g. read_skill("db_mnop")). No asking first.
4. Use the schema from the skill to write correct SQL.
5. Call query_database(connection_id="<connection_name>", sql="SELECT ...").
   The connection_id is the part after "db_" in the skill name (e.g. skill "db_mnop" → connection_id "mnop").
6. Only SELECT queries. No INSERT, UPDATE, DELETE, DROP.
7. Single value result → return as text. Multiple rows → Excel file attachment.

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

━━━ TELEGRAM MESSAGES ━━━
Messages tagged [via Telegram] come from the user's phone.
For short conversational replies (status updates, confirmations, quick answers) — keep it brief and plain text, no markdown tables or heavy formatting.
For anything the user asked you to create or produce (drafts, documents, lists, code, analysis) — deliver it in full, with normal formatting, without shortening or summarising.
The tag is silent context only. Do not mention Telegram or acknowledge the channel.
CRITICAL: When replying to a [via Telegram] message, respond DIRECTLY with your answer — do NOT call telegram_send. The system handles delivery automatically. telegram_send is ONLY for proactive notifications (automations, reminders, unprompted alerts). Never call telegram_send as a response to a user message.

━━━ WHATSAPP INTERACTIVE MESSAGES ━━━
Messages tagged [via WhatsApp interactive] come from a WhatsApp group conversation.
The tag includes [chat_id: ...] — that is the ONLY chat_id you must use for ALL replies and file sends in this conversation.
CRITICAL rules:
- Text replies: whatsapp_send(chat_id=<chat_id from tag>, message=<reply>)
- File sends: whatsapp_send_file(chat_id=<chat_id from tag>, file_path=<path>)
- NEVER call telegram_send or telegram_send_file
- "here", "send here", "send it here" — always means the chat_id from the tag, no confirmation needed
- NEVER ask the user where to send when they say "here" — just send to the tag chat_id
- If the user explicitly names OTHER groups/contacts to also send to, use whatsapp_get_groups to resolve their chat_ids and send to those too
- Default (no target specified) = tag chat_id only. Explicit named targets = send to those in addition
Keep replies concise and plain — no heavy markdown, no tables. WhatsApp renders plain text best.
The tag is silent context only. Do not mention WhatsApp or acknowledge the channel in your reply.

━━━ AUTOMATION RUNS ━━━
When triggered by an automation (cron job, email, file event), the trigger context
is provided at the top of the message. Read it carefully and act on it directly.
For email triggers: the full email is provided — read it, do not call gmail_read again.
For file triggers: the file path is provided — call read_file on that exact path.
For cron triggers: execute the task immediately, do not wait for confirmation.

CRITICAL — TOOL CALLS VS NARRATION:
If your instructions say to call telegram_ask or any other tool — INVOKE IT as a tool call.
NEVER narrate what you "will do" or "have done" instead of doing it.
WRONG: "I'll send you the draft via Telegram now."
WRONG: "I've sent the draft for your approval."
RIGHT: [actually invoke telegram_ask tool with the draft text in the question argument]
This rule is absolute. Every tool mentioned in your instructions must be called, not described.

━━━ MULTI-AGENT ORCHESTRATION ━━━
Spawn workers ONLY for these exact patterns — no others:

  PATTERN 1 — Multiple recipients, same content
    Condition: sending the same content to N≥2 recipients (email, Telegram, etc.)
    Action: YOU prepare the content first, then spawn one worker per recipient.
    Example: "email summary to A, B, C" → you write summary → spawn 3 workers each with gmail_send

  PATTERN 2 — Multiple independent deliveries after a single result
    Condition: you have a finished result AND N≥2 independent delivery tasks
    (e.g. save to file + send email + send Telegram)
    Action: spawn one worker per delivery, give each the exact content to deliver.
    Example: "store summary as txt AND email it AND send to Telegram" → spawn 3 workers

  PATTERN 3 — Search/read N≥4 files independently
    Condition: user asks to read, analyze, or extract from 4+ separate files where
    each file's result is independent (not building on previous results)
    Action: spawn one worker per file or batch of 3 files.
    NOTE: for RAG (rag_ingest + rag_search), do NOT spawn workers — call RAG tools directly.

  PATTERN 4 — Multiple independent browser/Playwright tasks
    Condition: user asks for N≥2 independent browser tasks in parallel.
    Action: spawn one worker per task immediately. Each worker gets its own self-contained task_description.
    tools_allowed: ["mcp__playwright__browser_navigate", "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_click", "mcp__playwright__browser_type", "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_wait_for", "write_file", "telegram_send", "telegram_send_file", "gmail_send"]
    Do NOT ask for clarification. Just spawn.

In ALL other cases, do the work yourself sequentially. Do not invent parallelism.

Worker rules:
  - YOU handle all thinking, summarizing, and personalizing BEFORE spawning.
  - Workers only do pure execution (send, save, fetch, read, browse). No reasoning needed from them.
  - Give each worker a self-contained task_description with the exact content — no ambiguity.
  - Always list the exact tools_allowed each worker needs (e.g. ["gmail_send"]).
  - Call spawn_workers ONCE with ALL workers in a single call.
  - Workers cannot spawn sub-workers.
  - NEVER ask the user for the task format or JSON structure. You already know it: each task is
    {{"task_description": "...", "tools_allowed": ["tool1", "tool2"]}}. Just call spawn_workers_tool.

After workers finish:
  - Workers return a one-line summary each. READ those summaries.
  - YOU write the final reply to the user — clean, concise, no worker internal logs.
  - Format: brief intro line, then one bullet per worker: what it did + outcome.
  - NEVER paste raw worker output. NEVER repeat worker internal monologue.
  - If a worker failed, say so clearly in one line."""


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
- When done, respond with EXACTLY one line in this format (nothing else):
  DONE: <what you did> | <file path or "sent via telegram/email">
- No explanations, no multi-paragraph summaries. One line only."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Screenshot auto-relocation: Playwright MCP saves PNGs into
# <project>/.playwright-mcp/ (hardcoded by the server). We intercept the tool
# result, move the file into D:\screenshots\, and rewrite the ToolMessage so
# the agent only ever sees the final workspace path.
import re as _re
import shutil as _shutil
from pathlib import Path as _Path

_SCREENSHOT_DST_DIR = _Path(r"D:\screenshots")
# Match Windows absolute paths that pass through ".playwright-mcp" and end in
# .png. Allow spaces/most characters in intermediate segments, but stop at
# quotes, angle brackets, pipes, or a newline — things that can't appear in a
# real path. We anchor on the ".playwright-mcp" literal so we don't grab random
# unrelated paths.
_PLAYWRIGHT_PATH_RE = _re.compile(
    r"([A-Za-z]:[\\/][^\r\n\"'<>|*?]*?\.playwright-mcp[\\/][^\r\n\"'<>|*?]+?\.png)",
    _re.IGNORECASE,
)

# Generic filename extracted from a Playwright result that only mentions the
# basename ("saved to mrbeast_video.png"). We search likely dump dirs for a
# matching file created very recently.
_BARE_PNG_RE = _re.compile(r"([A-Za-z0-9_\-\. ]+\.png)", _re.IGNORECASE)

# Directories Playwright / MCP servers have been observed dumping PNGs into.
# We scan these after any screenshot call to catch stragglers that our path
# regex missed.
_PROJECT_ROOT = _Path(__file__).resolve().parent.parent.parent
_SCREENSHOT_SCAN_DIRS = (
    _PROJECT_ROOT,
    _PROJECT_ROOT / ".playwright-mcp",
    _Path.cwd(),
)


_PLAYWRIGHT_DEAD_SESSION_RE = _re.compile(
    r"Target page, context or browser has been closed", _re.IGNORECASE
)


def _annotate_playwright_error(tool_name: str, result_str: str) -> str:
    """If a Playwright tool reports a dead-session error, append a recovery
    hint so the agent knows to call browser_close first (instead of retrying
    the same call forever)."""
    if not tool_name.startswith("mcp__playwright__"):
        return result_str
    if tool_name == "mcp__playwright__browser_close":
        return result_str
    if not _PLAYWRIGHT_DEAD_SESSION_RE.search(result_str):
        return result_str
    return (
        result_str
        + "\n\n[System hint: the Playwright browser session died. "
        "Call mcp__playwright__browser_close, then retry browser_navigate ONCE. "
        "Do not retry the failing tool more than twice.]"
    )


def _move_one_screenshot(src: _Path) -> _Path | None:
    """Copy src into the screenshots dir, delete source, return new path."""
    try:
        _SCREENSHOT_DST_DIR.mkdir(parents=True, exist_ok=True)
        dst = _SCREENSHOT_DST_DIR / src.name
        if dst.exists():
            stem, suffix = dst.stem, dst.suffix
            for i in range(1, 1000):
                candidate = _SCREENSHOT_DST_DIR / f"{stem}_{i}{suffix}"
                if not candidate.exists():
                    dst = candidate
                    break
        _shutil.copy2(str(src), str(dst))
        try:
            src.unlink()
        except OSError:
            pass
        logger.info("Screenshot relocated: %s -> %s", src, dst)
        return dst
    except Exception as exc:
        logger.warning("Screenshot move failed (%s): %s", src, exc)
        return None


# Known legitimate files in the project root — never touched by the sweep.
_PROJECT_ROOT_WHITELIST = {
    "CLAUDE.md", "README.md", "planchat.txt", "requirements.txt",
    ".env", ".env.example", ".gitignore", "run.py", "app.db",
}


def _sweep_playwright_artifacts() -> None:
    """Remove stray .md / .yml / .yaml / .png files Playwright MCP dumps into
    the project root (browser_snapshot writes markdown accessibility trees,
    some tools write yaml configs). Whitelist protects real project files.
    Safe to call after any Playwright tool invocation."""
    try:
        import time as _time
        now = _time.time()
        for p in _PROJECT_ROOT.iterdir():
            if not p.is_file():
                continue
            if p.name in _PROJECT_ROOT_WHITELIST:
                continue
            if p.suffix.lower() not in (".md", ".yml", ".yaml"):
                continue
            try:
                if now - p.stat().st_mtime > 120:
                    continue  # only sweep recent dumps
                p.unlink()
                logger.info("Swept stray Playwright artifact: %s", p.name)
            except OSError:
                pass
    except Exception as exc:
        logger.warning("Playwright artifact sweep failed: %s", exc)


def _relocate_playwright_screenshot(result_str: str) -> str:
    """Find a screenshot PNG the tool just created and move it to
    D:\\screenshots\\. Tries three strategies:
      1. Explicit .playwright-mcp\\...\\foo.png path in the result.
      2. Bare "foo.png" filename — scan likely dump dirs for it.
      3. Fallback: newest .png under 30s old in scan dirs.
    On success, rewrites the tool result to reference the new path so the
    agent cannot reference the temp path. Input is returned unchanged on
    total failure.
    """
    try:
        # Strategy 1: explicit full path in the result
        match = _PLAYWRIGHT_PATH_RE.search(result_str)
        if match:
            src = _Path(match.group(1))
            if src.exists() and src.is_file():
                dst = _move_one_screenshot(src)
                if dst:
                    return f"Screenshot saved to {dst}. Use telegram_send_file(\"{dst}\") to deliver it."

        # Strategy 2: bare filename in the result
        bare_match = _BARE_PNG_RE.search(result_str)
        if bare_match:
            fname = bare_match.group(1).strip()
            for d in _SCREENSHOT_SCAN_DIRS:
                cand = d / fname
                if cand.exists() and cand.is_file() and cand.resolve() != _SCREENSHOT_DST_DIR.resolve() / fname:
                    # Skip files already in the destination
                    try:
                        cand.resolve().relative_to(_SCREENSHOT_DST_DIR.resolve())
                        continue  # already in dst
                    except ValueError:
                        pass
                    dst = _move_one_screenshot(cand)
                    if dst:
                        return f"Screenshot saved to {dst}. Use telegram_send_file(\"{dst}\") to deliver it."

        # Strategy 3: newest .png created in the last 30 seconds in any scan dir
        import time as _time
        now = _time.time()
        newest: _Path | None = None
        newest_mtime = 0.0
        for d in _SCREENSHOT_SCAN_DIRS:
            if not d.exists():
                continue
            try:
                for p in d.iterdir():
                    if not p.is_file() or p.suffix.lower() != ".png":
                        continue
                    try:
                        p.resolve().relative_to(_SCREENSHOT_DST_DIR.resolve())
                        continue  # skip files already in dst
                    except ValueError:
                        pass
                    mt = p.stat().st_mtime
                    if now - mt > 30:
                        continue
                    if mt > newest_mtime:
                        newest_mtime = mt
                        newest = p
            except OSError:
                continue
        if newest:
            dst = _move_one_screenshot(newest)
            if dst:
                return f"Screenshot saved to {dst}. Use telegram_send_file(\"{dst}\") to deliver it."

        return result_str
    except Exception as exc:
        logger.warning("Screenshot relocation failed: %s", exc)
        return result_str


_ERROR_MARKERS = ("MCP tool error", "Tool error:", "Tool timed out", "Error:")


# ---- Fix 1+2+3 helpers: multi-task detection + narration stripping ---------

_NARRATION_PATTERNS = _re.compile(
    r"\b(i'?ll (?:report|get back|update|send|share|let you know|do that|start|spawn|run|begin)"
    r"|i'?ve (?:started|spawned|kicked off|begun|launched|dispatched|sent|initiated)"
    r"|spawn(?:ed|ing) (?:the |\d+ )?(?:worker|task|job|agent|browser)"
    r"|working on (?:it|that|these|this)(?: now| in parallel)?"
    r"|(?:starting|running|executing|dispatching|kicking off) (?:the |\d+ |these |now)"
    r"|on it(?:!|,| now)"
    r"|once (?:they|the workers|the tasks) (?:finish|complete|are done))",
    _re.IGNORECASE,
)

# A user turn is "multi-task browser-ish" when it has ≥2 bullets/lines that
# describe independent browser-like actions (open/search/screenshot/send/save).
_MULTITASK_BULLET_RE = _re.compile(
    r"(?mi)^\s*(?:[-*•]|\d+[.)])\s*(?=.{0,200}?\b("
    r"open|navigate|search|screenshot|take\s+a\s+screenshot|send\s+(?:it|to)|save|"
    r"email|mail|telegram|download|scrape|visit|go\s+to)\b)",
)

# Action verbs that mark an independent task line even without a bullet.
_MULTITASK_VERB_RE = _re.compile(
    r"(?mi)^\s*(open|navigate|search|take\s+a\s+screenshot|visit|go\s+to)\b",
)


def _is_multitask_request(text: str) -> bool:
    """Heuristic: does this user message describe ≥2 independent browser/tool tasks?"""
    if not text or len(text) < 40:
        return False
    bullets = len(_MULTITASK_BULLET_RE.findall(text))
    if bullets >= 2:
        return True
    verbs = len(_MULTITASK_VERB_RE.findall(text))
    return verbs >= 2


def _strip_premature_narration(msg: AIMessage) -> AIMessage:
    """Fix 1: if an AIMessage has tool_calls AND chit-chat content, blank the content.

    The chit-chat streams to the UI before workers finish, confusing the user.
    We keep the tool call intact so the graph still routes to workers/tools.
    """
    if not isinstance(msg, AIMessage):
        return msg
    if not getattr(msg, "tool_calls", None):
        return msg
    content = msg.content
    if not content:
        return msg
    text = content if isinstance(content, str) else str(content)
    if not text.strip():
        return msg
    if _NARRATION_PATTERNS.search(text) or len(text) < 400:
        # Short explanatory chatter that precedes a tool call → strip.
        # (We keep content only if it's a long, substantive answer that
        # happens to include a tool call — rare but possible.)
        try:
            msg.content = ""
        except Exception:
            pass
    return msg


def _last_human_text(messages: list[BaseMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            c = m.content
            return c if isinstance(c, str) else str(c)
    return ""


async def _ainvoke_with_retry(runnable, payload, *, attempts: int = 3):
    """Invoke an LLM runnable with retry on transient errors (429 / 5xx / network).

    Backoff: 1s, 3s. Does not retry on 400 (bad request / schema violation),
    which is a bug to fix, not a flaky call.
    """
    import asyncio as _asyncio
    delays = [1.0, 3.0]
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await runnable.ainvoke(payload)
        except Exception as exc:
            msg = str(exc)
            name = type(exc).__name__
            transient = (
                "RateLimit" in name
                or "APIConnection" in name
                or "APITimeout" in name
                or "ServiceUnavailable" in name
                or " 429" in msg or " 500" in msg or " 502" in msg
                or " 503" in msg or " 504" in msg
            )
            if not transient or i == attempts - 1:
                raise
            last_exc = exc
            logger.warning("LLM transient error (attempt %d/%d): %s", i + 1, attempts, exc)
            await _asyncio.sleep(delays[min(i, len(delays) - 1)])
    if last_exc:
        raise last_exc


def _detect_stuck_loop(messages: list[BaseMessage]) -> str | None:
    """Return a stop message if the same tool has failed N times in a row.

    Walks backward through the last few AIMessage+ToolMessage pairs. If the
    same tool name shows up with an error-prefixed result 3+ times in a row,
    we refuse to let the supervisor dispatch another call and return a
    terminal message for the user.

    The LLM can't be trusted to cap retries on its own — LLMs are stubborn
    when they think a retry will work. This breaks the loop in code.
    """
    # Collect the sequence of (tool_name, is_error) for recent tool calls,
    # newest first. Walk backward: for each AIMessage with tool_calls, pair
    # it with the following ToolMessage(s).
    recent: list[tuple[str, bool]] = []
    # Build a map of tool_call_id -> ToolMessage content to match pairs
    tc_results: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            tc_results[m.tool_call_id] = str(getattr(m, "content", "") or "")

    # Collect (tool_name, content) for each completed call, in order
    completed: list[tuple[str, str]] = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in getattr(m, "tool_calls", None) or []:
                result = tc_results.get(tc["id"])
                if result is None:
                    continue
                completed.append((tc["name"], result))

    if len(completed) < 3:
        return None

    # Check the last 3 calls — same-tool-repeat
    last3 = completed[-3:]
    names = {n for n, _ in last3}
    all_errors_3 = all(any(marker in r for marker in _ERROR_MARKERS) for _, r in last3)

    if len(names) == 1 and all_errors_3:
        tool_name = last3[0][0]
        last_err = last3[-1][1]
        err_summary = last_err[:250].replace("\n", " ")
        return (
            f"Gave up after 3 failed attempts of {tool_name}. "
            f"Last error: {err_summary}\n\n"
            "Tell the user concisely what failed (one sentence) and ask if they "
            "want you to try a different approach. Do NOT call any tool in your "
            "next response — just reply in plain text."
        )

    # Alternating-tools-same-error: last 5 calls all errored, any mix of tools.
    if len(completed) >= 5:
        last5 = completed[-5:]
        if all(any(marker in r for marker in _ERROR_MARKERS) for _, r in last5):
            err_summary = last5[-1][1][:250].replace("\n", " ")
            return (
                "Gave up after 5 consecutive tool failures. "
                f"Last error: {err_summary}\n\n"
                "Tell the user concisely what failed and ask if they want a "
                "different approach. Do NOT call any tool in your next response — "
                "just reply in plain text."
            )

    return None


def _heal_dangling_tool_calls(messages: list[BaseMessage]) -> None:
    """Fix broken message history in-place by injecting synthetic ToolMessages.

    If an AIMessage with tool_calls exists but some tool_call_ids have no
    corresponding ToolMessage response, OpenAI returns a 400 error. This
    function finds those gaps and fills them so the thread can continue.
    """
    responded_ids: set[str] = set()
    for m in messages:
        if isinstance(m, ToolMessage):
            responded_ids.add(m.tool_call_id)

    for i, m in enumerate(messages):
        if not isinstance(m, AIMessage):
            continue
        tool_calls = getattr(m, "tool_calls", None) or []
        missing = [tc for tc in tool_calls if tc["id"] not in responded_ids]
        if not missing:
            continue
        # Insert synthetic ToolMessages right after this AIMessage
        synthetic = [
            ToolMessage(
                tool_call_id=tc["id"],
                content="[Tool call interrupted — result unavailable. Please retry if needed.]",
            )
            for tc in missing
        ]
        insert_at = i + 1
        for j, sm in enumerate(synthetic):
            messages.insert(insert_at + j, sm)
            responded_ids.add(sm.tool_call_id)
        logger.warning(
            "_heal_dangling_tool_calls: injected %d synthetic ToolMessages for %s",
            len(synthetic), [tc["name"] for tc in missing],
        )


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

    # Filter tools (include MCP tools in worker pool)
    mcp_tools = await load_active_mcp_tools()
    all_worker_tools = WORKER_TOOLS + mcp_tools
    if tools_allowed:
        active_tools = [t for t in all_worker_tools if t.name in tools_allowed]
    else:
        active_tools = all_worker_tools

    llm = ChatOpenAI(
        model=model_name,
        api_key=app_config.OPENAI_API_KEY,
        streaming=True,
    )
    llm_with_tools = llm.bind_tools(active_tools)

    # Cap worker context to last 20 messages
    worker_messages = list(state["messages"])
    _WINDOW = 20
    if len(worker_messages) > _WINDOW:
        non_sys = [m for m in worker_messages if not isinstance(m, SystemMessage)]
        sys_msgs = [m for m in worker_messages if isinstance(m, SystemMessage)]
        # Preserve the last HumanMessage across windowing (same fix as supervisor).
        last_human_idx = next(
            (i for i in range(len(non_sys) - 1, -1, -1) if isinstance(non_sys[i], HumanMessage)),
            None,
        )
        windowed = non_sys[-_WINDOW:]
        if last_human_idx is not None and not any(m is non_sys[last_human_idx] for m in windowed):
            windowed = [non_sys[last_human_idx]] + windowed
        worker_messages = sys_msgs + windowed

    # Drop only truly orphaned ToolMessages (whose AIMessage parent is gone).
    # Valid AIMessage+ToolMessage groups without a preceding HumanMessage are
    # still legal for OpenAI — do not strip them.
    sys_msgs = [m for m in worker_messages if isinstance(m, SystemMessage)]
    non_sys_part = [m for m in worker_messages if not isinstance(m, SystemMessage)]
    while non_sys_part and isinstance(non_sys_part[0], ToolMessage):
        non_sys_part.pop(0)
    known_tc_ids: set[str] = set()
    for m in non_sys_part:
        if isinstance(m, AIMessage):
            for tc in getattr(m, "tool_calls", None) or []:
                known_tc_ids.add(tc["id"])
    non_sys_part = [
        m for m in non_sys_part
        if not (isinstance(m, ToolMessage) and m.tool_call_id not in known_tc_ids)
    ]
    worker_messages = sys_msgs + non_sys_part

    response: BaseMessage = await _ainvoke_with_retry(llm_with_tools, worker_messages)

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
    # Tools explicitly listed in tools_allowed are pre-approved by the supervisor —
    # skip the interrupt/ask flow for them so workers don't deadlock inside gather().
    worker_tools_allowed: list[str] = cfg.get("worker_tools_allowed", [])

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

        # Tools explicitly listed in tools_allowed are pre-approved by the supervisor.
        # Workers run inside asyncio.gather and cannot be individually interrupted.
        if tool_name in worker_tools_allowed:
            decision = "auto"

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
            logger.info(
                "Permission resume: tool=%s decision=%s thread=%s",
                tool_name, user_decision, thread_id,
            )

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

        # Execute — fall back to MCP tools if not in static map
        t = _TOOL_MAP.get(tool_name)
        if t is None:
            mcp_tools = await load_active_mcp_tools()
            mcp_map = {mt.name: mt for mt in mcp_tools}
            t = mcp_map.get(tool_name)
        if t is None:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Unknown tool: {tool_name}")
            )
            continue

        try:
            result = await asyncio.wait_for(
                t.ainvoke(tool_args),
                timeout=120.0,
            )

            result_str = str(result)
            if tool_name == "mcp__playwright__browser_take_screenshot":
                result_str = _relocate_playwright_screenshot(result_str)
            if tool_name.startswith("mcp__playwright__"):
                _sweep_playwright_artifacts()
            result_str = _annotate_playwright_error(tool_name, result_str)
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

        except asyncio.TimeoutError:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Tool timed out after 120s: {tool_name}")
            )
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

class WorkerTask(BaseModel):
    task_description: str = Field(description="Full self-contained instructions for the worker — what to do, what URLs to open, what files to save, what tools to call.")
    tools_allowed: list[str] = Field(description="Exact tool names the worker may use, e.g. ['mcp__playwright__navigate', 'mcp__playwright__screenshot', 'write_file', 'telegram_send']")


@tool
def spawn_workers_tool(tasks: list[WorkerTask]) -> str:
    """Spawn multiple worker agents in parallel to execute independent tasks.

    Call this ONCE with ALL workers together. Each WorkerTask has:
    - task_description: full self-contained instructions (URL, actions, output location)
    - tools_allowed: exact list of tool names the worker needs

    Example:
      tasks=[
        WorkerTask(
          task_description="Open https://youtube.com, search 'MrBeast', take screenshot, save to workspace/screenshots/mrbeast.png",
          tools_allowed=["mcp__playwright__browser_navigate","mcp__playwright__browser_take_screenshot","mcp__playwright__browser_type","mcp__playwright__browser_click","write_file"]
        ),
        WorkerTask(
          task_description="Open https://platform.openai.com/docs/models, take screenshot, save to workspace/screenshots/openai.png",
          tools_allowed=["mcp__playwright__browser_navigate","mcp__playwright__browser_take_screenshot","write_file"]
        ),
      ]

    This is intercepted by run_workers_node — the body never executes.
    """
    # This body is never actually executed — run_workers_node intercepts it.
    return json.dumps({"error": "spawn_workers_tool invoked directly — should be intercepted"})


# Full tool list for supervisor (includes spawn_workers)
SUPERVISOR_TOOLS = [
    read_file,
    write_file,
    clear_file,
    copy_file,
    move_file,
    create_folder,
    find_file,
    list_dir,
    delete_file,
    run_shell_command,
    web_search,
    web_fetch,
    *GOOGLE_TOOLS,
    telegram_send,
    telegram_ask,
    save_draft,
    schedule_message,
    telegram_send_file,
    whatsapp_send,
    whatsapp_send_file,
    whatsapp_read_messages,
    whatsapp_fetch_messages,
    whatsapp_get_groups,
    generate_image,
    read_skill,
    rag_ingest,
    rag_search,
    query_database,
    spawn_workers_tool,
]

_SUPERVISOR_TOOL_MAP: dict[str, Any] = {t.name: t for t in SUPERVISOR_TOOLS}


# ---------------------------------------------------------------------------
# Supervisor nodes
# ---------------------------------------------------------------------------

def _should_continue(state: AgentState) -> str:
    last: BaseMessage = state["messages"][-1]
    if not (hasattr(last, "tool_calls") and last.tool_calls):
        logger.info("_should_continue: no tool_calls → END (type=%s content_preview=%s)", type(last).__name__, str(getattr(last, "content", ""))[:80])
        return END
    # Route: if any call is spawn_workers_tool, go to workers node
    for tc in last.tool_calls:
        if tc["name"] == "spawn_workers_tool":
            return "workers"
    logger.info("_should_continue: routing to tools for %s", [tc["name"] for tc in last.tool_calls])
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

    # Per-turn reset: bounds are a budget for the CURRENT user turn, not a
    # lifetime counter for the thread. Reset whenever the latest message is a
    # HumanMessage — that always marks the start of a new turn.
    # start_time resets unconditionally so a slow prior turn never poisons the
    # 600s budget of the next one (e.g. after a permission-card approval where
    # an AIMessage already exists after the HumanMessage).
    msgs = state.get("messages") or []
    last_human_idx = next(
        (i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], HumanMessage)),
        None,
    )
    if last_human_idx is not None:
        has_ai_after = any(
            isinstance(m, AIMessage) for m in msgs[last_human_idx + 1:]
        )
        if not has_ai_after:
            ctx = RunContext(
                recursion_depth=0,
                agent_count=1,
                tool_call_count=0,
                start_time=time.monotonic(),
            )
        else:
            # New turn but AI already responded (e.g. post-approval resume) —
            # keep counters but always refresh the clock so 600s is per-turn.
            ctx = RunContext(
                recursion_depth=ctx.get("recursion_depth", 0),
                agent_count=ctx.get("agent_count", 1),
                tool_call_count=ctx.get("tool_call_count", 0),
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

    mcp_tools = await load_active_mcp_tools()
    all_supervisor_tools = SUPERVISOR_TOOLS + mcp_tools
    llm_with_tools = llm.bind_tools(all_supervisor_tools)

    memories_block = await _load_user_memories()
    skills_block = await _load_skills_index()
    messages = list(state["messages"])

    logger.debug(
        "supervisor_node: raw state has %d messages: %s",
        len(messages),
        [(type(m).__name__, getattr(m, "tool_calls", None) and "has_tc" or str(m.content)[:60]) for m in messages],
    )

    # Sliding window: keep system message + last ~20 messages to cap token usage.
    # CRITICAL: always preserve the most recent HumanMessage (the current turn's
    # user input) even if it would otherwise be trimmed out of the window.
    # Losing it causes the "Could you please repeat your last request?" bug
    # because the supervisor has AI/Tool messages but no user ask to answer.
    # ALSO CRITICAL: never split an AIMessage+ToolMessage pair — if the window
    # starts mid-pair, walk forward until we're at a clean boundary.
    _WINDOW = 20
    if len(messages) > _WINDOW:
        non_system = [m for m in messages if not isinstance(m, SystemMessage)]
        last_human_idx = next(
            (i for i in range(len(non_system) - 1, -1, -1) if isinstance(non_system[i], HumanMessage)),
            None,
        )
        windowed = non_system[-_WINDOW:]
        # Walk forward past any leading ToolMessages that lost their AIMessage parent
        while windowed and isinstance(windowed[0], ToolMessage):
            windowed = windowed[1:]
        if (
            last_human_idx is not None
            and not any(m is non_system[last_human_idx] for m in windowed)
        ):
            # Prepend the last HumanMessage so the supervisor always sees what
            # the user actually asked. Accept exceeding _WINDOW by 1 here.
            windowed = [non_system[last_human_idx]] + windowed
        messages = windowed

    # Drop invalid sequences from the front of the window:
    # - Orphan ToolMessages (their AIMessage+tool_calls parent is gone)
    # OpenAI requires every ToolMessage to follow an AIMessage with matching
    # tool_call_ids. An AIMessage+tool_calls group WITH its ToolMessages is
    # valid even without a preceding HumanMessage — do not strip those.
    before_strip = len(messages)
    # Step 1: drop leading orphan ToolMessages (no matching AIMessage ahead of them).
    known_tc_ids: set[str] = set()
    while messages and isinstance(messages[0], ToolMessage):
        if messages[0].tool_call_id in known_tc_ids:
            break  # shouldn't happen but be safe
        messages.pop(0)
    # Step 2: collect tool_call_ids declared by AIMessages that remain.
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in getattr(m, "tool_calls", None) or []:
                known_tc_ids.add(tc["id"])
    # Step 3: drop any remaining ToolMessages whose tool_call_id is unknown.
    messages = [
        m for m in messages
        if not (isinstance(m, ToolMessage) and m.tool_call_id not in known_tc_ids)
    ]
    if len(messages) != before_strip:
        logger.warning(
            "supervisor_node: stripped %d orphan tool messages, %d remain, first=%s",
            before_strip - len(messages), len(messages),
            type(messages[0]).__name__ if messages else "EMPTY",
        )

    # If stripping left no messages at all, the thread state is unrecoverable.
    if not messages:
        logger.warning("supervisor_node: no messages after strip — thread state corrupt, injecting recovery prompt")
        messages = [HumanMessage(content="[System: previous message context was lost due to a state error. Please ask the user to repeat their last request.]")]
    # If we still have AI/Tool messages but no HumanMessage in view, the window
    # scrolled past the original user request. Inject a neutral continuation
    # prompt instead of the recovery text (which causes the LLM to apologise
    # and ask the user to repeat themselves).
    elif not any(isinstance(m, HumanMessage) for m in messages):
        logger.warning("supervisor_node: no HumanMessage in window — injecting continuation prompt")
        messages = [HumanMessage(content="Continue with the task using the recent tool results above. Produce the final deliverable or the next required tool call.")] + messages

    # Hard-stop if the same tool has failed 3+ times in a row — the LLM won't
    # cap retries reliably on its own, and this is what produces the infinite
    # loop that eventually hits LangGraph's recursion limit.
    stop_notice = _detect_stuck_loop(messages)
    if stop_notice is not None:
        logger.warning("supervisor_node: stuck-loop detected — forcing stop")
        messages = messages + [SystemMessage(content=stop_notice)]

    system = SystemMessage(content=_supervisor_system_prompt() + memories_block + skills_block)
    messages.insert(0, system)

    # Heal any remaining dangling tool calls deeper in the history.
    _heal_dangling_tool_calls(messages)

    # Fix 2: per-turn nudge when the user message looks like ≥2 parallel tasks,
    # BUT only if workers haven't already run this turn.
    user_text = _last_human_text(messages)
    is_multitask = _is_multitask_request(user_text)
    workers_already_ran = any(
        isinstance(m, ToolMessage) and "workers finished" in (getattr(m, "content", "") or "").lower()
        for m in messages
    )
    if is_multitask and not workers_already_ran:
        messages.append(SystemMessage(content=(
            "REMINDER: The user asked for multiple independent tasks in one turn. "
            "You MUST call spawn_workers_tool on this turn with one WorkerTask per item. "
            "Do NOT reply in plain text. Do NOT say 'I'll report back' or 'on it'. "
            "Emit the tool call now."
        )))

    # If workers just finished, bind LLM without tools so it can't spawn again
    if workers_already_ran:
        response = await _ainvoke_with_retry(llm.bind_tools([]), messages)
    else:
        response = await _ainvoke_with_retry(llm_with_tools, messages)

    # Fix 3: if this was a multitask turn but the model replied with text and no
    # tool calls, reject and retry once with a sharper nudge.
    # Only applies if workers haven't already run.
    if (
        is_multitask
        and not workers_already_ran
        and isinstance(response, AIMessage)
        and not getattr(response, "tool_calls", None)
    ):
        logger.warning("supervisor_node: multitask turn returned no tool_calls — retrying once")
        messages.append(SystemMessage(content=(
            "You just replied in plain text. That is wrong. "
            "The user asked for multiple parallel tasks. "
            "Call spawn_workers_tool NOW with one WorkerTask per task. No prose."
        )))
        response = await _ainvoke_with_retry(llm_with_tools, messages)

    # Fix 1: strip premature "I'll report back" chatter from tool-calling messages.
    response = _strip_premature_narration(response) if isinstance(response, AIMessage) else response

    logger.info(
        "supervisor_node: model=%s tool_calls=%s content_preview=%s",
        model_name,
        [tc["name"] for tc in (getattr(response, "tool_calls", None) or [])],
        (getattr(response, "content", "") or "")[:120],
    )

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
        logger.info("supervisor policy_tools: tool=%s tool_call_count=%d", tool_name, ctx["tool_call_count"])
        err = _check_bounds(ctx)
        if err:
            logger.warning("supervisor policy_tools: BOUNDS CHECK FAILED tool=%s err=%s", tool_name, err)
            tool_messages.append(ToolMessage(tool_call_id=tool_call_id, content=err))
            continue

        decision = get_decision(tool_name, tool_args)
        logger.info("supervisor policy_tools: tool=%s decision=%s", tool_name, decision)

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
            logger.info(
                "Permission resume: tool=%s decision=%s thread=%s",
                tool_name, user_decision, thread_id,
            )

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
            # Fall back to MCP tools
            mcp_tools = await load_active_mcp_tools()
            mcp_map = {mt.name: mt for mt in mcp_tools}
            t = mcp_map.get(tool_name)
        if t is None:
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Unknown tool: {tool_name}")
            )
            continue

        logger.info("supervisor policy_tools: EXECUTING tool=%s args=%s", tool_name, str(tool_args)[:120])
        try:
            result = await asyncio.wait_for(
                t.ainvoke(tool_args, config=config),
                timeout=120.0,
            )
            result_str = str(result)
            if tool_name == "mcp__playwright__browser_take_screenshot":
                result_str = _relocate_playwright_screenshot(result_str)
            if tool_name.startswith("mcp__playwright__"):
                _sweep_playwright_artifacts()
            result_str = _annotate_playwright_error(tool_name, result_str)
            logger.info("supervisor policy_tools: DONE tool=%s result_preview=%s", tool_name, result_str[:120])
            tool_messages.append(ToolMessage(tool_call_id=tool_call_id, content=result_str))
        except asyncio.TimeoutError:
            logger.warning("supervisor policy_tools: TIMEOUT tool=%s", tool_name)
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"Tool timed out after 120s: {tool_name}")
            )
        except Exception as exc:
            logger.warning("supervisor policy_tools: ERROR tool=%s exc=%s", tool_name, exc)
            tool_messages.append(
                ToolMessage(tool_call_id=tool_call_id, content=f"MCP tool error ({tool_name}): {exc}")
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
        raw_tasks = raw_args.get("tasks", [])
        # Normalise: Pydantic objects → dicts
        tasks: list[dict] = [
            t.model_dump() if hasattr(t, "model_dump") else t
            for t in raw_tasks
        ]

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
        worker_ids = [str(uuid.uuid4())[:8] for _ in tasks]
        try:
            from app.web.routes.ws import manager as ws_manager
            # Fix 4: sticky banner so users know work is in flight even if
            # individual worker events are missed (reconnects etc).
            await ws_manager.send(thread_id, {
                "type": "workers_banner",
                "status": "running",
                "count": len(tasks),
                "text": f"Running {len(tasks)} worker{'s' if len(tasks) != 1 else ''} in parallel…",
            })
            for wid, task in zip(worker_ids, tasks):
                await ws_manager.send(thread_id, {
                    "type": "worker_start",
                    "worker_id": wid,
                    "description": task.get("task_description", "")[:120],
                })
        except Exception:
            pass

        # Send a status notification (NOT a token — tokens get saved to DB and
        # confuse the supervisor into thinking it needs to act again)
        try:
            from app.web.routes.ws import manager as ws_manager
            await ws_manager.send(thread_id, {
                "type": "worker_status",
                "content": f"Running {len(tasks)} workers in parallel...",
            })
        except Exception:
            pass

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
                raw = last_ai.content if last_ai else "Worker completed with no output."
                # Keep only the DONE: line — strip verbose internal monologue
                lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
                done_lines = [l for l in lines if l.upper().startswith("DONE:")]
                result_text = done_lines[-1] if done_lines else lines[-1] if lines else "No output."

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

        # Fix 4: replace the "running" banner with a "done" marker.
        try:
            from app.web.routes.ws import manager as ws_manager
            await ws_manager.send(thread_id, {
                "type": "workers_banner",
                "status": "done",
                "count": len(results),
                "text": f"{len(results)} worker{'s' if len(results) != 1 else ''} finished.",
            })
        except Exception:
            pass

        # Build worker containers — each has a summary (one line) and detail (full).
        # Supervisor receives ONLY summaries. Details are stored for reference but
        # never sent to the LLM — they are the "container contents" the worker filled.
        containers: list[dict] = []
        for i, (wid, result_text) in enumerate(results, 1):
            lines = [l.strip() for l in result_text.strip().splitlines() if l.strip()]
            done_lines = [l for l in lines if l.upper().startswith("DONE:")]
            summary = done_lines[-1] if done_lines else (lines[-1] if lines else "No output.")
            containers.append({
                "worker": i,
                "summary": summary,
                "detail": result_text,  # stored but NOT sent to supervisor LLM
            })

        # What the supervisor LLM sees: numbered summaries only + hard instruction
        summary_lines = "\n".join(f"[Worker {c['worker']}] {c['summary']}" for c in containers)
        tool_msg_content = (
            f"All {len(containers)} workers finished. Summaries:\n\n"
            f"{summary_lines}\n\n"
            "INSTRUCTION: Write your final reply to the user now. "
            "One clean paragraph. Cover what each worker did and the result. "
            "Do NOT call any more tools. Do NOT spawn more workers."
        )
        tool_messages.append(ToolMessage(tool_call_id=tool_call_id, content=tool_msg_content))

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
# Module-level singletons
# ---------------------------------------------------------------------------

_checkpointer: AsyncSqliteSaver | None = None
_graph: Any | None = None


def get_graph() -> Any:
    if _graph is None:
        raise RuntimeError("LangGraph supervisor not initialised — did on_startup run?")
    return _graph


async def init_supervisor() -> None:
    global _checkpointer, _graph, _worker_graph
    import aiosqlite
    # Use a dedicated SQLite file for LangGraph checkpoints — avoids asyncpg
    # event-loop conflicts on Windows (asyncpg vs psycopg can't share the same
    # SelectorEventLoop cleanly on Python 3.14).
    conn = await aiosqlite.connect("checkpoints.db")
    _checkpointer = AsyncSqliteSaver(conn=conn)
    await _checkpointer.setup()
    _graph = _build_graph(_checkpointer)
    _worker_graph = _build_worker_graph()


async def shutdown_supervisor() -> None:
    global _checkpointer, _graph, _worker_graph
    _graph = None
    _worker_graph = None
    if _checkpointer is not None:
        try:
            await _checkpointer.conn.close()
        except Exception:
            pass
        _checkpointer = None
