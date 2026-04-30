"""run_python — execute a Python script in workspace/tmp/ with 60s timeout."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Annotated

import app.config as app_config
from langchain_core.tools import tool


_TIMEOUT_SECONDS = 60


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
    workspace = app_config.WORKSPACE_DIR
    tmp_dir = workspace / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    script_path = tmp_dir / f"raion_script_{int(time.time() * 1000)}.py"
    script_path.write_text(code, encoding="utf-8")

    # Snapshot workspace files before run to detect new ones
    before = set(_iter_workspace_files(workspace))

    try:
        result = subprocess.run(
            ["python", str(script_path)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            cwd=str(workspace.parent),  # project root so 'workspace/file' paths resolve
        )
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

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    parts = [f"Exit: {result.returncode}"]
    if stdout:
        parts.append(f"Output:\n{stdout}")
    if stderr:
        parts.append(f"Stderr:\n{stderr}")
    parts.append(f"Files created:\n  {new_files_str}")
    parts.append(f"Script: {script_path}")

    return "\n".join(parts)


def _iter_workspace_files(workspace: Path):
    """Yield relative string paths of all files under workspace (excluding tmp scripts)."""
    try:
        for p in workspace.rglob("*"):
            if p.is_file() and not (p.parts[-2] == "tmp" and p.name.startswith("raion_script_")):
                yield str(p.relative_to(workspace.parent))
    except Exception:
        return
