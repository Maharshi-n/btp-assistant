"""Phase 5: allowlist-gated shell command tool.

Only commands whose base command (first token) is on the allowlist are permitted.
Everything else raises NotAllowlistedError so the LLM sees a clear refusal.
"""
from __future__ import annotations

import asyncio
import shlex
from typing import Annotated

from langchain_core.tools import tool


class NotAllowlistedError(ValueError):
    """Raised when the requested command is not on the allowlist."""


# ---------------------------------------------------------------------------
# Allowlist — exact base-command strings that are permitted.
# This is an allowlist (not a denylist): anything not here is blocked.
# ---------------------------------------------------------------------------
_ALLOWED_BASE_COMMANDS: frozenset[str] = frozenset(
    [
        "ls",
        "dir",
        "cat",
        "type",
        "git",   # only "git status / log / diff" sub-commands — checked below
        "python",
        "pwd",
        "whoami",
        "echo",
    ]
)

# For "git", only these sub-commands are allowed
_ALLOWED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(["status", "log", "diff"])

# Timeout in seconds for any shell command
_TIMEOUT_SECONDS = 30


def _validate_command(command: str) -> None:
    """Raise NotAllowlistedError if *command* is not permitted."""
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise NotAllowlistedError(f"Could not parse command: {e}")

    if not tokens:
        raise NotAllowlistedError("Empty command.")

    base = tokens[0].lower()
    # Strip path prefix (e.g. /usr/bin/git → git)
    base = base.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    if base not in _ALLOWED_BASE_COMMANDS:
        raise NotAllowlistedError(
            f"Command '{base}' is not on the allowlist. "
            f"Allowed commands: {', '.join(sorted(_ALLOWED_BASE_COMMANDS))}."
        )

    # Extra check for git: only status / log / diff
    if base == "git":
        if len(tokens) < 2 or tokens[1].lower() not in _ALLOWED_GIT_SUBCOMMANDS:
            sub = tokens[1] if len(tokens) >= 2 else "<none>"
            raise NotAllowlistedError(
                f"'git {sub}' is not allowed. "
                f"Only: git {', git '.join(sorted(_ALLOWED_GIT_SUBCOMMANDS))}."
            )


@tool
def run_shell_command(
    command: Annotated[str, "Shell command to run. Must be on the allowlist."],
) -> str:
    """Run an allowlisted read-only shell command and return its output.

    Allowed commands: ls, dir, cat, type, git status/log/diff,
    python --version, pwd, whoami, echo.

    Any command not on the allowlist is blocked automatically.
    """
    try:
        _validate_command(command)
    except NotAllowlistedError as e:
        return f"Blocked: {e}"

    try:
        result = asyncio.get_event_loop().run_until_complete(
            _run_async(command)
        )
        return result
    except RuntimeError:
        # If there's already a running event loop (typical in FastAPI), fall
        # back to a synchronous subprocess call.
        import subprocess

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
            )
            output = proc.stdout
            if proc.returncode != 0 and proc.stderr:
                output += f"\n[stderr]: {proc.stderr}"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {_TIMEOUT_SECONDS} seconds."
        except OSError as e:
            return f"Error running command: {e}"


async def _run_async(command: str) -> str:
    """Async helper that actually runs the subprocess."""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        return f"Error: command timed out after {_TIMEOUT_SECONDS} seconds."

    output = stdout.decode(errors="replace")
    if proc.returncode != 0 and stderr:
        output += f"\n[stderr]: {stderr.decode(errors='replace')}"
    return output or "(no output)"
