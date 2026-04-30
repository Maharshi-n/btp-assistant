"""run_python — execute a Python script in workspace/tmp/ with 60s timeout."""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import app.config as app_config
from langchain_core.tools import tool


_TIMEOUT_SECONDS = 60
_MAX_OUTPUT = 20_000  # ~20 KB

# Patterns that are always blocked regardless of permission policy.
_DANGEROUS_CODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bos\.system\s*\(", re.IGNORECASE),
    re.compile(r"\bshutil\.rmtree\s*\(", re.IGNORECASE),
    re.compile(
        r"\bsubprocess\.(run|call|Popen|check_output)\s*\(.*[\"\']\s*(rm|del|format|shutdown)",
        re.IGNORECASE | re.DOTALL,
    ),
)


@tool
def run_python(
    code: Annotated[str, "Python code to execute. Use workspace-relative paths like 'workspace/file.xlsx'."],
) -> str:
    """Execute Python code for data tasks: merging files, filtering rows, aggregating, transforming.

    - Writes code to workspace/tmp/raion_script_<timestamp>.py and runs it
    - 60-second timeout — long-running scripts are killed
    - pip install lines in your code work (e.g. 'import subprocess; subprocess.run([...])')
    - Returns exit code, stdout, stderr, and list of new files created in workspace
    - On error: read the stderr, fix the code, call run_python again

    Example:
        import pandas as pd
        df = pd.read_excel('workspace/sales.xlsx')
        result = df[df['status'] == 'active']
        result.to_excel('workspace/active_sales.xlsx', index=False)
        print(f'Done: {len(result)} rows written to workspace/active_sales.xlsx')
    """
    # Safety check — block dangerous patterns before touching disk
    for pat in _DANGEROUS_CODE_PATTERNS:
        if pat.search(code):
            return (
                f"Exit: 1\n"
                f"Error: Code blocked by safety policy — pattern '{pat.pattern}' matched. "
                "Use run_shell_command for shell operations."
            )

    workspace = app_config.WORKSPACE_DIR
    tmp_dir = workspace / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    script_path = tmp_dir / f"raion_script_{int(time.time() * 1000)}.py"
    script_path.write_text(code, encoding="utf-8")

    # Snapshot workspace files before run to detect new ones
    before = set(_iter_workspace_files(workspace))

    try:
        returncode, stdout, stderr = asyncio.get_event_loop().run_until_complete(
            _run_python_async(script_path, workspace)
        )
    except subprocess.TimeoutExpired:
        # Async path timed out (raised by _run_python_async after killing the proc)
        return (
            f"Exit: 1\n"
            f"Error: Script timed out after {_TIMEOUT_SECONDS} seconds and was killed.\n"
            f"Files created: none\n"
            f"Script saved at: {script_path} (deleted)"
        )
    except RuntimeError:
        # Already inside a running event loop (FastAPI) — fall back to sync subprocess
        try:
            result = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
                cwd=str(workspace.parent),
            )
            returncode = result.returncode
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
        except subprocess.TimeoutExpired:
            script_path.unlink(missing_ok=True)
            return (
                f"Exit: 1\n"
                f"Error: Script timed out after {_TIMEOUT_SECONDS} seconds and was killed.\n"
                f"Files created: none\n"
                f"Script saved at: {script_path} (deleted)"
            )

    after = set(_iter_workspace_files(workspace))
    new_files = sorted(after - before)
    new_files_str = "\n  ".join(new_files) if new_files else "none"

    # Cap output size
    if len(stdout) > _MAX_OUTPUT:
        stdout = stdout[:_MAX_OUTPUT] + f"\n[truncated — {len(stdout):,} bytes total]"
    if len(stderr) > _MAX_OUTPUT:
        stderr = stderr[:_MAX_OUTPUT] + f"\n[truncated — {len(stderr):,} bytes total]"

    parts = [f"Exit: {returncode}"]
    if stdout:
        parts.append(f"Output:\n{stdout}")
    if stderr:
        parts.append(f"Stderr:\n{stderr}")
    parts.append(f"Files created:\n  {new_files_str}")
    parts.append(f"Script: {script_path}")

    return "\n".join(parts)


async def _run_python_async(script_path: Path, workspace: Path) -> tuple[int, str, str]:
    """Async helper that spawns the script subprocess and enforces the timeout."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=str(workspace.parent),
    )
    try:
        raw_stdout, raw_stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        script_path.unlink(missing_ok=True)
        raise subprocess.TimeoutExpired(cmd=str(script_path), timeout=_TIMEOUT_SECONDS)

    stdout = raw_stdout.decode(errors="replace").strip()
    stderr = raw_stderr.decode(errors="replace").strip()
    return proc.returncode, stdout, stderr


def _iter_workspace_files(workspace: Path):
    """Yield relative string paths of all files under workspace (excluding tmp scripts)."""
    try:
        for p in workspace.rglob("*"):
            if p.is_file() and not (p.parts[-2] == "tmp" and p.name.startswith("raion_script_")):
                yield str(p.relative_to(workspace.parent))
    except Exception:
        return
