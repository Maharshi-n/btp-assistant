"""Phase 6: Permission policy.

One function per tool that decides whether a tool call is "auto" (execute
immediately) or "ask" (pause graph and request user approval).

Policy summary (from the plan's Permission Model section):
  - Reads/lists inside workspace          → auto
  - Writes inside workspace               → auto for NEW files, ask for overwrites
  - Deletes + overwrites of existing      → always ask, even inside workspace
  - Shell commands (allowlisted)          → auto (already gated by allowlist)
  - Web search / web fetch                → auto  (network reads)
  - Network writes (email/calendar/drive) → always ask  [future phases]

Every call here that returns "ask" will cause the supervisor to call
LangGraph interrupt(), which pauses and checkpoints the graph until the
user approves or denies from the UI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import app.config as app_config

Decision = Literal["auto", "ask"]


# ---------------------------------------------------------------------------
# Public helpers — called from the tool wrappers in supervisor.py
# ---------------------------------------------------------------------------

def policy_read_file(args: dict) -> Decision:
    """Reads are always auto inside the workspace (OutsideWorkspaceError handles the rest)."""
    return "auto"


def policy_list_dir(args: dict) -> Decision:
    """Directory listings are always auto."""
    return "auto"


def policy_write_file(args: dict) -> Decision:
    """New files → auto.  Overwriting an existing file → ask."""
    path_str: str = args.get("path", "")
    workspace = app_config.WORKSPACE_DIR
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if resolved.exists():
        return "ask"
    return "auto"


def policy_delete_file(args: dict) -> Decision:
    """Deletes are always ask — even inside the workspace."""
    return "ask"


_SHELL_ASK_PATTERNS = (
    # Destructive / system-modifying — always ask
    r"\brm\b", r"\brmdir\b", r"\bdel\b", r"\berase\b",
    r"Remove-Item", r"\bmv\b\s.*\s/", r"\bmove\b",
    r"\buninstall\b", r"winget\s+uninstall",
    r"pip\s+uninstall", r"npm\s+uninstall", r"yarn\s+remove",
    r"git\s+push\s+.*--force", r"git\s+push\s+.*-f\b",
    r"git\s+reset\s+--hard", r"git\s+clean\s+-[a-z]*f",
    r"git\s+branch\s+-D\b",
    r"\bkill\b", r"Stop-Process", r"taskkill",
    r"\bchmod\b", r"\bchown\b", r"icacls", r"takeown",
    r"DROP\s+TABLE", r"DROP\s+DATABASE", r"TRUNCATE\s+TABLE",
    r"\bsudo\b", r"runas\s+/",
    r"reg\s+(delete|add)\b", r"regedit",
    r"\bcurl\b.*\|\s*(sh|bash|powershell|pwsh|cmd)",
    r"Invoke-Expression", r"\biex\b", r"\bsc\s+(delete|stop|create)\b",
    r">\s*[A-Za-z]:[\\/]",  # redirect to absolute path (overwrite)
    r"echo\s+.*>\s*[A-Za-z]:[\\/]",
)
import re as _sh_re
_SHELL_ASK_RE = _sh_re.compile("|".join(_SHELL_ASK_PATTERNS), _sh_re.IGNORECASE)


def policy_run_shell_command(args: dict) -> Decision:
    """Auto for read-only / constructive commands; ask for anything destructive
    (delete, uninstall, force-push, kill, chmod, registry edits, sudo, pipe-to-shell, etc.).
    """
    command = args.get("command", "") or ""
    if _SHELL_ASK_RE.search(command):
        return "ask"
    return "auto"


def policy_web_search(args: dict) -> Decision:
    """Web searches are always auto (read-only network op)."""
    return "auto"


def policy_web_fetch(args: dict) -> Decision:
    """Web fetches are always auto (read-only network op)."""
    return "auto"


# ---------------------------------------------------------------------------
# Phase 8 — Google tool policies
# ---------------------------------------------------------------------------

def policy_gmail_list_unread(args: dict) -> Decision:
    """Reading email is auto (read-only)."""
    return "auto"


def policy_gmail_read(args: dict) -> Decision:
    """Reading a single email is auto (read-only)."""
    return "auto"


def policy_gmail_search(args: dict) -> Decision:
    """Searching email is auto (read-only)."""
    return "auto"


def policy_gmail_send(args: dict) -> Decision:
    """Sending email is always ask (network write)."""
    return "ask"


def policy_drive_list(args: dict) -> Decision:
    """Listing Drive files is auto (read-only)."""
    return "auto"


def policy_drive_read(args: dict) -> Decision:
    """Reading a Drive file is auto (read-only)."""
    return "auto"


def policy_drive_write(args: dict) -> Decision:
    """Writing to Drive is always ask (network write)."""
    return "ask"


def policy_drive_download(args: dict) -> Decision:
    """Downloading from Drive to workspace is auto (read from Drive, write to local workspace)."""
    return "auto"


def policy_drive_upload(args: dict) -> Decision:
    """Uploading from workspace to Drive is always ask (network write)."""
    return "ask"


def policy_calendar_list_events(args: dict) -> Decision:
    """Listing calendar events is auto (read-only)."""
    return "auto"


def policy_calendar_create_event(args: dict) -> Decision:
    """Creating a calendar event is always ask (network write)."""
    return "ask"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def policy_telegram_send(args: dict) -> Decision:
    """Sending a Telegram notification to yourself is auto."""
    return "auto"


def policy_telegram_ask(args: dict) -> Decision:
    """Asking a question via Telegram is auto — it's a notification to self."""
    return "auto"


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

def policy_whatsapp_send(args: dict) -> Decision:
    """Sending a WhatsApp message requires owner approval by default."""
    return "ask"


def policy_whatsapp_send_file(args: dict) -> Decision:
    return "ask"


def policy_whatsapp_read_messages(args: dict) -> Decision:
    return "auto"


def policy_whatsapp_fetch_messages(args: dict) -> Decision:
    return "auto"


def policy_whatsapp_get_groups(args: dict) -> Decision:
    return "auto"


# ---------------------------------------------------------------------------
# RAG tools
# ---------------------------------------------------------------------------

def policy_rag_ingest(args: dict) -> Decision:
    """RAG ingestion reads files and writes to local vector store — auto."""
    return "auto"


def policy_rag_search(args: dict) -> Decision:
    """RAG search is a local read-only operation — auto."""
    return "auto"


# ---------------------------------------------------------------------------
# Dispatch table — maps tool name → policy function
# ---------------------------------------------------------------------------

_POLICY_TABLE: dict[str, object] = {
    "read_file": policy_read_file,
    "write_file": policy_write_file,
    "list_dir": policy_list_dir,
    "delete_file": policy_delete_file,
    "run_shell_command": policy_run_shell_command,
    "web_search": policy_web_search,
    "web_fetch": policy_web_fetch,
    # Phase 8 — Google tools
    "gmail_list_unread": policy_gmail_list_unread,
    "gmail_read": policy_gmail_read,
    "gmail_search": policy_gmail_search,
    "gmail_send": policy_gmail_send,
    "drive_list": policy_drive_list,
    "drive_read": policy_drive_read,
    "drive_write": policy_drive_write,
    "drive_download": policy_drive_download,
    "drive_upload": policy_drive_upload,
    "calendar_list_events": policy_calendar_list_events,
    "calendar_create_event": policy_calendar_create_event,
    "telegram_send": policy_telegram_send,
    "telegram_ask": policy_telegram_ask,
    "whatsapp_send": policy_whatsapp_send,
    "whatsapp_send_file": policy_whatsapp_send_file,
    "whatsapp_read_messages": policy_whatsapp_read_messages,
    "whatsapp_fetch_messages": policy_whatsapp_fetch_messages,
    "whatsapp_get_groups": policy_whatsapp_get_groups,
    # Skills — read-only, always auto
    "read_skill": lambda args: "auto",
    "save_draft": lambda args: "auto",
    "schedule_message": lambda args: "auto",
    # RAG — local vector store operations
    "rag_ingest": policy_rag_ingest,
    "rag_search": policy_rag_search,
}


def get_decision(tool_name: str, args: dict) -> Decision:
    """Return the policy decision for a given tool and its arguments.

    MCP tools (prefixed mcp__) look up their permission from the DB.
    Falls back to "ask" for unknown tools (safe default).
    """
    fn = _POLICY_TABLE.get(tool_name)
    if fn is not None:
        return fn(args)  # type: ignore[operator]

    if tool_name.startswith("mcp__"):
        return _mcp_tool_decision(tool_name)

    return "ask"


def _mcp_tool_decision(tool_name: str) -> Decision:
    """Look up MCP tool permission from DB synchronously via a cached map."""
    try:
        import asyncio
        from app.db.engine import AsyncSessionLocal
        from app.db.models import MCPTool
        from sqlalchemy import select as sa_select

        async def _fetch():
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    sa_select(MCPTool).where(MCPTool.name == tool_name)
                )
                row = result.scalars().first()
                if row is None:
                    return "ask"
                return row.permission  # "auto" | "ask"

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — schedule and wait
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, _fetch())
                    return future.result(timeout=2)
            else:
                return loop.run_until_complete(_fetch())
        except Exception:
            return "ask"
    except Exception:
        return "ask"


def human_readable_prompt(tool_name: str, args: dict) -> str:
    """Return a short human-readable approval prompt for the UI card."""
    if tool_name == "run_shell_command":
        cmd = args.get("command", "?")
        if len(cmd) > 200:
            cmd = cmd[:200] + "…"
        return f"Run shell command: {cmd}"
    if tool_name == "delete_file":
        path = args.get("path", "?")
        return f"Delete file: {path}"
    if tool_name == "write_file":
        path = args.get("path", "?")
        return f"Overwrite existing file: {path}"
    if tool_name == "gmail_send":
        to = args.get("to", "?")
        subject = args.get("subject", "?")
        return f"Send email to {to} — Subject: {subject}"
    if tool_name == "drive_write":
        name = args.get("name", "?")
        return f"Write file to Google Drive: {name}"
    if tool_name == "drive_upload":
        file_path = args.get("file_path", "?")
        name = args.get("name", "") or file_path
        return f"Upload '{name}' to Google Drive"
    if tool_name == "calendar_create_event":
        summary = args.get("summary", "?")
        start = args.get("start", "?")
        return f"Create calendar event: {summary} at {start}"
    # Generic fallback
    arg_str = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    return f"Run {tool_name}({arg_str})"
