# run_python Tool + Data Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `run_python` tool that lets the agent execute pandas/Python scripts for file merging, filtering, and transformation tasks — with a 60-second timeout, automatic pip install, and structured output for the observation loop.

**Architecture:** A new `app/tools/python_runner.py` exposes the `run_python` tool. It writes code to a temp `.py` file in `workspace/tmp/`, runs it via subprocess with a 60s timeout, captures stdout/stderr/exit_code, and detects any new files created in workspace. A companion skill file `workspace/skills/python_data_ops.md` tells the agent when and how to use it. The tool is wired into `supervisor.py` and `policy.py`.

**Tech Stack:** Python stdlib (`subprocess`, `pathlib`, `time`), pandas, openpyxl (both already installed), LangChain `@tool` decorator.

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `app/tools/python_runner.py` | Create | `run_python` tool implementation |
| `app/permissions/policy.py` | Modify | Add `run_python` → `auto` policy entry |
| `app/agents/supervisor.py` | Modify | Import + register `run_python` in WORKER_TOOLS + system prompt |
| `workspace/skills/python_data_ops.md` | Create | Skill file teaching agent when/how to use `run_python` |

---

### Task 1: Create `run_python` tool

**Files:**
- Create: `app/tools/python_runner.py`
- Test: run manually via Python REPL (no pytest — tool is a thin wrapper around subprocess)

- [ ] **Step 1: Write the tool file**

Create `app/tools/python_runner.py` with this exact content:

```python
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
```

- [ ] **Step 2: Verify the tool loads without error**

Run from project root:
```bash
python -c "from app.tools.python_runner import run_python; print('ok')"
```
Expected output: `ok`

- [ ] **Step 3: Smoke test — run a simple script**

```bash
python -c "
import asyncio
from app.tools.python_runner import run_python
result = run_python.invoke({'code': 'print(\"hello from script\")'})
print(result)
"
```
Expected output contains:
```
Exit: 0
Output:
hello from script
```

- [ ] **Step 4: Test timeout kills the script**

```bash
python -c "
from app.tools.python_runner import run_python
result = run_python.invoke({'code': 'import time; time.sleep(999)'})
print(result)
"
```
Expected output contains: `timed out after 60 seconds`

- [ ] **Step 5: Test new file detection**

```bash
python -c "
from app.tools.python_runner import run_python
code = '''
import pathlib
pathlib.Path('workspace/tmp/test_detect.txt').write_text('hello')
print('wrote file')
'''
result = run_python.invoke({'code': code})
print(result)
"
```
Expected output: `Files created:` section lists `workspace/tmp/test_detect.txt`

- [ ] **Step 6: Commit**

```bash
git add app/tools/python_runner.py
git commit -m "feat: add run_python tool with 60s timeout and file detection"
```

---

### Task 2: Wire `run_python` into permissions and supervisor

**Files:**
- Modify: `app/permissions/policy.py` (add policy entry)
- Modify: `app/agents/supervisor.py` (import, WORKER_TOOLS, system prompt)

- [ ] **Step 1: Add policy entry in `app/permissions/policy.py`**

Find the `_POLICY_TABLE` dict (around line 219). Add one line after the `query_database` entry:

```python
    # Database queries — SELECT-only enforced in execute_query, always auto
    "query_database": lambda args: "auto",
    # Python runner — executes scripts in workspace/tmp/, always auto
    "run_python": lambda args: "auto",
```

- [ ] **Step 2: Import `run_python` in `app/agents/supervisor.py`**

Find the imports block (around line 77):
```python
from app.tools.database import query_database
```
Add directly below it:
```python
from app.tools.python_runner import run_python
```

- [ ] **Step 3: Add `run_python` to `WORKER_TOOLS` list in `app/agents/supervisor.py`**

Find `WORKER_TOOLS = [` list (around line 140). Find `query_database` at the end of the list and add `run_python` right after it:

```python
    query_database,
    run_python,
]
```

- [ ] **Step 4: Add `run_python` to the system prompt tool listing**

Find the `━━━ TOOLS AVAILABLE ━━━` section in the system prompt (around line 283). Find the `Databases` line:

```
Databases  : query_database(connection_id, sql)  — run SELECT queries against connected DBs
```

Replace it with:

```
Databases  : query_database(connection_id, sql)  — run SELECT queries against connected DBs
Python     : run_python(code)  — execute Python/pandas scripts for file merging, filtering, transforming
             writes script to workspace/tmp/, 60s timeout, returns stdout + new files created
             use for ANY task that involves combining/processing files — do NOT do this via LLM
```

- [ ] **Step 5: Verify supervisor imports cleanly**

```bash
python -c "from app.agents.supervisor import build_graph; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add app/permissions/policy.py app/agents/supervisor.py
git commit -m "feat: register run_python in WORKER_TOOLS and policy (auto permission)"
```

---

### Task 3: Create `python_data_ops` skill file

**Files:**
- Create: `workspace/skills/python_data_ops.md`

The skill file must also be registered in the DB so the agent's skills index picks it up. We do this by inserting a row into the `skills` table via the app's DB session.

- [ ] **Step 1: Create the skill markdown file**

Create `workspace/skills/python_data_ops.md` with this exact content:

```markdown
# Python Data Operations

Use `run_python` whenever the task involves:
- Merging or joining two or more files (Excel, CSV)
- Filtering rows by condition
- Aggregating/grouping data (sum, count, average by category)
- Deduplicating records
- Matching records between a database query result and a local file
- Any transformation that would be lossy or error-prone if done by the LLM directly

## Rules

1. **Never transform tabular data by reading it into the LLM context.** Use `run_python` instead.
2. Always `print()` the row count and output file path at the end of the script so the observation loop can verify success.
3. If `run_python` returns `Exit: 1`, read the `Stderr:` section, fix the code, and call `run_python` again.
4. File paths in scripts should be relative to the project root: `workspace/filename.xlsx`

## Standard Imports

```python
import pandas as pd
import pathlib
```

## Patterns

### Read Excel / CSV
```python
df = pd.read_excel('workspace/file.xlsx')      # Excel
df = pd.read_csv('workspace/file.csv')         # CSV
```

### Filter rows
```python
result = df[df['status'] == 'active']
```

### Merge two files (left join on a key column)
```python
df1 = pd.read_excel('workspace/sales.xlsx')
df2 = pd.read_excel('workspace/customers.xlsx')
merged = df1.merge(df2, on='customer_id', how='left')
merged.to_excel('workspace/merged_output.xlsx', index=False)
print(f'Done: {len(merged)} rows written to workspace/merged_output.xlsx')
```

### Aggregate / group by
```python
result = df.groupby('region')['revenue'].sum().reset_index()
result.to_excel('workspace/revenue_by_region.xlsx', index=False)
print(f'Done: {len(result)} regions in workspace/revenue_by_region.xlsx')
```

### Match DB query result with local file
```python
# query_database saves result to workspace/tmp/query_result_<ts>.xlsx
# pass that path to run_python
db_df = pd.read_excel('workspace/tmp/query_result_1234567890.xlsx')
local_df = pd.read_excel('workspace/local_users.xlsx')
matched = db_df.merge(local_df, on='email', how='inner')
matched.to_excel('workspace/matched_users.xlsx', index=False)
print(f'Matched: {len(matched)} users in workspace/matched_users.xlsx')
```

### Install a missing package (add at top of script if needed)
```python
import subprocess
subprocess.run(['pip', 'install', 'some-package', '-q'], check=True)
import some_package
```
```

- [ ] **Step 2: Register the skill in the DB**

Run this from the project root (one-time setup, safe to re-run):

```bash
python -c "
import asyncio
from app.db.engine import AsyncSessionLocal
from app.db.models import Skill
from sqlalchemy import select

async def register():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Skill).where(Skill.name == 'python_data_ops'))
        existing = result.scalars().first()
        if existing:
            existing.trigger_description = 'use when merging, filtering, transforming, or aggregating files or data'
            existing.file_path = 'skills/python_data_ops.md'
            existing.enabled = True
        else:
            db.add(Skill(
                name='python_data_ops',
                trigger_description='use when merging, filtering, transforming, or aggregating files or data',
                file_path='skills/python_data_ops.md',
                enabled=True,
            ))
        await db.commit()
        print('Skill registered ok')

asyncio.run(register())
"
```
Expected: `Skill registered ok`

- [ ] **Step 3: Verify skill appears in the skills list**

Start the app (`python run.py`) and navigate to `/skills` — `python_data_ops` should appear in the list as enabled.

- [ ] **Step 4: Commit**

```bash
git add workspace/skills/python_data_ops.md
git commit -m "feat: add python_data_ops skill file for run_python guidance"
```

---

### Task 4: End-to-end test

**Files:** none — manual verification only

- [ ] **Step 1: Start the app**

```bash
python run.py
```

- [ ] **Step 2: Upload a test Excel file**

Create a small test file `workspace/test_data.xlsx` with columns `name, score` and a few rows. You can do this via the agent: "create a file workspace/test_data.xlsx with columns name and score, 5 rows of sample data using run_python"

- [ ] **Step 3: Ask the agent to filter it**

Send in chat: "filter workspace/test_data.xlsx to only rows where score > 50 and save as workspace/high_scores.xlsx"

Expected:
- Agent calls `read_skill("python_data_ops")` 
- Agent calls `run_python` with pandas filter code
- `run_python` returns `Exit: 0` and `Files created: workspace/high_scores.xlsx`
- Agent reports file path

- [ ] **Step 4: Test error + retry loop**

Send in chat: "merge workspace/test_data.xlsx with workspace/nonexistent.xlsx on column name"

Expected:
- Agent writes merge script
- `run_python` returns `Exit: 1` with `FileNotFoundError` in stderr
- Agent fixes the script or reports the error clearly — does NOT hallucinate a result

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: verify run_python end-to-end merge/filter/error flow"
```
