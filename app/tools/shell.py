"""Shell command tool — unrestricted but with a destructive-action safety rule.

The agent may run any command. However, it MUST ask the user before running
anything destructive (deleting files/folders, force-pushing git, dropping
databases, killing processes, formatting drives, uninstalling packages, etc.).

Hard-blocked at the tool layer (bypass the model entirely): a small denylist of
catastrophic commands we never want to run from the agent, regardless of what
the permission policy decides.
"""
from __future__ import annotations

import asyncio
import re
from typing import Annotated

from langchain_core.tools import tool

# Timeout in seconds for any shell command
_TIMEOUT_SECONDS = 120

# Commands we refuse outright. Matching is substring + word-boundary aware.
# These are checked AFTER the permission policy — so even if the user clicked
# "approve", these still don't execute.
_HARD_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-[rf]+\s+[\"']?/\S*", re.IGNORECASE),        # rm -rf /...
    re.compile(r"\brm\s+-[rf]+\s+[\"']?[A-Za-z]:[\\/]", re.IGNORECASE),  # rm -rf C:\ D:\
    re.compile(r"\bmkfs\b", re.IGNORECASE),                          # filesystem format
    re.compile(r"\bformat\s+[A-Za-z]:", re.IGNORECASE),              # Windows format X:
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),   # fork bomb
    re.compile(r"\bdd\b[^|]*\bof=/dev/(sd[a-z]|nvme|disk)", re.IGNORECASE),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b", re.IGNORECASE),
    re.compile(r"\bcipher\s+/w", re.IGNORECASE),                     # Windows secure-wipe
    re.compile(r"\bdel\s+/[a-z]?/?\s*[A-Za-z]:[\\/]", re.IGNORECASE),  # del /s /q C:\
    re.compile(r"Remove-Item.*-Recurse.*(-Force)?\s+[\"']?[A-Za-z]:[\\/]", re.IGNORECASE),
    re.compile(r"netsh\s+advfirewall", re.IGNORECASE),               # firewall tampering
    re.compile(r"\biptables\s+-F\b", re.IGNORECASE),
)


def _hard_block_reason(command: str) -> str | None:
    for pat in _HARD_BLOCK_PATTERNS:
        if pat.search(command):
            return pat.pattern
    return None


@tool
def run_shell_command(
    command: Annotated[str, "Shell command to run in the system terminal."],
) -> str:
    """Run any shell command and return its output.

    SAFETY RULE: Before running anything destructive (rm -rf, drop database,
    force push, kill process, uninstall, format, etc.) — stop and ask the user
    for explicit confirmation first. For read-only or constructive commands,
    proceed directly.
    """
    blocked = _hard_block_reason(command)
    if blocked:
        return (
            f"Command blocked by safety policy (pattern: {blocked}). "
            "This command was refused at the tool layer — change the approach."
        )

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
                stdin=subprocess.DEVNULL,
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
        stdin=asyncio.subprocess.DEVNULL,
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
