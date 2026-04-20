# Multi-Workspace Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a primary + N secondary workspace locations so the agent can search across all allowed paths, write only to writable ones, and save to the primary by default.

**Architecture:** A new `WorkspaceLocation` DB table holds all locations (one primary, rest secondary). `filesystem.py` expands `_safe_resolve()` to accept any allowed root and adds a `find_file` tool that searches primary-first. The settings page gets a minimal disclosure UI for managing locations. On first startup the existing `WORKSPACE_DIR` is seeded as the initial primary row.

**Tech Stack:** Python/FastAPI, SQLAlchemy async (AsyncSession) + sync (SyncSessionLocal), Jinja2/HTMX, Tailwind CSS, PostgreSQL

---

## File Map

| Action | File | Purpose |
|--------|------|---------|
| Modify | `app/db/models.py` | Add `WorkspaceLocation` model |
| Modify | `app/db/seed.py` | Seed primary location from `WORKSPACE_DIR` on first startup |
| Modify | `app/main.py` | Import `WorkspaceLocation` so SQLAlchemy registers it |
| Modify | `app/tools/filesystem.py` | Expand `_safe_resolve()`, add `find_file` tool |
| Create | `app/web/routes/workspaces.py` | CRUD API for workspace locations |
| Modify | `app/web/routes/settings.py` | Pass `secondary_workspaces` to template; update `update_workspace` to upsert DB row |
| Modify | `app/web/templates/settings.html` | Add disclosure section under primary workspace input |

---

## Task 1: Add `WorkspaceLocation` model

**Files:**
- Modify: `app/db/models.py`

- [ ] **Step 1: Add the model at the bottom of models.py**

Open `app/db/models.py` and append this class after the last existing model:

```python
class WorkspaceLocation(Base):
    __tablename__ = "workspace_locations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)
    writable: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: Verify the model is importable**

```bash
cd "E:/BTP project" && python -c "from app.db.models import WorkspaceLocation; print('ok')"
```

Expected output: `ok`

- [ ] **Step 3: Register in main.py imports**

In `app/main.py`, find the line:
```python
from app.db.models import Automation, AutomationConversation, ...
```
Add `WorkspaceLocation` to that import list (alphabetical order isn't required — just add it).

- [ ] **Step 4: Verify app starts and creates table**

```bash
cd "E:/BTP project" && python -c "
import asyncio
from app.db.engine import init_db
asyncio.run(init_db())
print('table created ok')
"
```

Expected output: `table created ok`

- [ ] **Step 5: Commit**

```bash
git -C "E:/BTP project" add app/db/models.py app/main.py
git -C "E:/BTP project" commit -m "feat: add WorkspaceLocation model"
```

---

## Task 2: Seed primary workspace location on startup

**Files:**
- Modify: `app/db/seed.py`

- [ ] **Step 1: Add seed function to seed.py**

Replace the entire contents of `app/db/seed.py` with:

```python
from __future__ import annotations

import os
from pathlib import Path

import bcrypt
from sqlalchemy import select

from app.config import ADMIN_PASSWORD, ADMIN_USERNAME, WORKSPACE_DIR
from app.db.engine import AsyncSessionLocal
from app.db.models import User, WorkspaceLocation


async def seed_admin() -> None:
    """Insert the admin user on first startup if no user exists yet."""
    if not ADMIN_PASSWORD:
        raise RuntimeError(
            "ADMIN_PASSWORD is not set in .env. "
            "Please add it before starting the server."
        )

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User))
        existing = result.scalars().first()
        if existing is not None:
            return

        password_hash = bcrypt.hashpw(
            ADMIN_PASSWORD.encode(), bcrypt.gensalt()
        ).decode()
        user = User(username=ADMIN_USERNAME, password_hash=password_hash)
        session.add(user)
        await session.commit()


async def seed_primary_workspace() -> None:
    """Ensure the primary WorkspaceLocation row exists for WORKSPACE_DIR."""
    resolved = str(Path(os.path.realpath(str(WORKSPACE_DIR))))
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(WorkspaceLocation).where(WorkspaceLocation.is_primary == True)  # noqa: E712
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return
        session.add(WorkspaceLocation(
            path=resolved,
            label="Main workspace",
            is_primary=True,
            writable=True,
        ))
        await session.commit()
```

- [ ] **Step 2: Call seed_primary_workspace from main.py startup**

In `app/main.py`, find:
```python
from app.db.seed import seed_admin
```
Change to:
```python
from app.db.seed import seed_admin, seed_primary_workspace
```

Then in `on_startup`, find:
```python
    await seed_admin()
```
Add the call immediately after:
```python
    await seed_admin()
    await seed_primary_workspace()
```

- [ ] **Step 3: Verify seed runs**

```bash
cd "E:/BTP project" && python -c "
import asyncio
from app.db.engine import init_db
from app.db.seed import seed_primary_workspace
async def run():
    await init_db()
    await seed_primary_workspace()
    print('seed ok')
asyncio.run(run())
"
```

Expected: `seed ok`

- [ ] **Step 4: Verify row exists in DB**

```bash
cd "E:/BTP project" && python -c "
import asyncio
from sqlalchemy import select
from app.db.engine import AsyncSessionLocal
from app.db.models import WorkspaceLocation
async def run():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(WorkspaceLocation))).scalars().all()
        for r in rows:
            print(r.id, r.path, r.is_primary, r.writable)
asyncio.run(run())
"
```

Expected: one row printed with `is_primary=True` and your workspace path.

- [ ] **Step 5: Commit**

```bash
git -C "E:/BTP project" add app/db/seed.py app/main.py
git -C "E:/BTP project" commit -m "feat: seed primary WorkspaceLocation on startup"
```

---

## Task 3: Expand filesystem.py — multi-root `_safe_resolve` + `find_file` tool

**Files:**
- Modify: `app/tools/filesystem.py`

- [ ] **Step 1: Add imports at the top of filesystem.py**

After the existing imports block, add:

```python
from sqlalchemy import select as sa_select
from app.db.engine import SyncSessionLocal
from app.db.models import WorkspaceLocation
```

- [ ] **Step 2: Add `_get_allowed_roots()` helper after the imports**

Insert this function right before the `OutsideWorkspaceError` class:

```python
def _get_allowed_roots() -> list[tuple[Path, bool]]:
    """Return [(resolved_path, writable), ...] for all workspace locations.
    
    Primary location is always first in the list.
    """
    try:
        with SyncSessionLocal() as db:
            rows = db.execute(
                sa_select(WorkspaceLocation).order_by(
                    WorkspaceLocation.is_primary.desc(),
                    WorkspaceLocation.created_at
                )
            ).scalars().all()
        if rows:
            return [(Path(os.path.realpath(r.path)), r.writable) for r in rows]
    except Exception:
        pass
    # Fallback: use app_config.WORKSPACE_DIR if DB is unavailable
    return [(Path(os.path.realpath(str(app_config.WORKSPACE_DIR))), True)]
```

- [ ] **Step 3: Replace `_safe_resolve` with the multi-root version**

Replace the entire `_safe_resolve` function (lines ~21–61 in original) with:

```python
def _safe_resolve(path_str: str, require_writable: bool = False) -> Path:
    """Resolve *path_str* and verify it falls inside an allowed workspace location.

    If *require_writable* is True and the matching location is read-only,
    raises OutsideWorkspaceError.
    """
    if not path_str or not isinstance(path_str, str):
        raise OutsideWorkspaceError("Empty or invalid path.")

    if path_str.startswith("\\\\") or path_str.startswith("//"):
        raise OutsideWorkspaceError("UNC paths are not allowed.")
    if path_str.startswith("\\\\?\\") or path_str.startswith("\\\\.\\"):
        raise OutsideWorkspaceError("Device / extended-length paths are not allowed.")
    if ":" in path_str[2:]:
        raise OutsideWorkspaceError("Alternate data streams are not allowed.")

    roots = _get_allowed_roots()
    # Use primary root for relative-path resolution
    primary_root = roots[0][0]

    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = primary_root / candidate

    try:
        resolved = Path(os.path.realpath(str(candidate)))
    except OSError as e:
        raise OutsideWorkspaceError(f"Could not resolve path '{path_str}': {e}")

    for root_path, writable in roots:
        try:
            resolved.relative_to(root_path)
        except ValueError:
            continue
        # Path is inside this root
        if require_writable and not writable:
            raise OutsideWorkspaceError(
                f"'{path_str}' is in a read-only workspace location '{root_path}'. "
                "This location is read-only and cannot be written to."
            )
        return resolved

    raise OutsideWorkspaceError(
        f"Path '{path_str}' is outside all allowed workspace locations. "
        "I can only access files inside configured workspace directories."
    )
```

- [ ] **Step 4: Update write tools to pass `require_writable=True`**

Find each of these calls in filesystem.py and add `require_writable=True`:

In `write_file`:
```python
        resolved = _safe_resolve(path, require_writable=True)
```

In `create_folder`:
```python
        resolved = _safe_resolve(path, require_writable=True)
```

In `delete_file`:
```python
        resolved = _safe_resolve(path, require_writable=True)
```

In `copy_file` (the dst resolve only — src can be read-only):
```python
        src_resolved = _safe_resolve(src)
        dst_resolved = _safe_resolve(dst, require_writable=True)
```

In `move_file` (src must be writable to move from it; dst must be writable):
```python
        src_resolved = _safe_resolve(src, require_writable=True)
        dst_resolved = _safe_resolve(dst, require_writable=True)
```

- [ ] **Step 5: Add the `find_file` tool at the end of filesystem.py**

```python
@tool
def find_file(
    filename: Annotated[str, "Filename to search for (e.g. 'resume.pdf'). Case-insensitive on Windows."],
) -> str:
    """Search for a file by name across all workspace locations.
    Searches the primary workspace first, then secondary locations in order added.
    Returns the full path and which workspace it was found in, or a not-found message.
    """
    roots = _get_allowed_roots()
    filename_lower = filename.lower()
    results: list[str] = []

    for root_path, _writable in roots:
        if not root_path.exists():
            continue
        for dirpath, _dirs, files in os.walk(str(root_path)):
            for fname in files:
                if fname.lower() == filename_lower:
                    full = Path(dirpath) / fname
                    results.append(str(full))

    if not results:
        return f"File '{filename}' not found in any workspace location."
    if len(results) == 1:
        return f"Found: {results[0]}"
    lines = [f"Found {len(results)} matches:"]
    for p in results:
        lines.append(f"  {p}")
    return "\n".join(lines)
```

- [ ] **Step 6: Verify filesystem module imports cleanly**

```bash
cd "E:/BTP project" && python -c "from app.tools.filesystem import read_file, write_file, find_file; print('ok')"
```

Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git -C "E:/BTP project" add app/tools/filesystem.py
git -C "E:/BTP project" commit -m "feat: multi-root _safe_resolve and find_file tool"
```

---

## Task 4: Register `find_file` with the agent

**Files:**
- Modify: `app/agents/supervisor.py`

- [ ] **Step 1: Find where filesystem tools are imported in supervisor.py**

```bash
grep -n "filesystem" "E:/BTP project/app/agents/supervisor.py"
```

Note the import line (e.g. `from app.tools.filesystem import read_file, write_file, ...`).

- [ ] **Step 2: Add `find_file` to that import**

Open `app/agents/supervisor.py`, find the filesystem tools import line and add `find_file`:

```python
from app.tools.filesystem import (
    copy_file,
    create_folder,
    delete_file,
    find_file,
    list_dir,
    move_file,
    read_file,
    write_file,
)
```

- [ ] **Step 3: Add `find_file` to the tools list**

In supervisor.py, find the list where all tools are collected (search for `read_file` to find it). Add `find_file` to that list alongside the other filesystem tools.

- [ ] **Step 4: Verify supervisor imports cleanly**

```bash
cd "E:/BTP project" && python -c "from app.agents.supervisor import init_supervisor; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git -C "E:/BTP project" add app/agents/supervisor.py
git -C "E:/BTP project" commit -m "feat: register find_file tool with agent"
```

---

## Task 5: Workspace locations API routes

**Files:**
- Create: `app/web/routes/workspaces.py`

- [ ] **Step 1: Create the routes file**

Create `app/web/routes/workspaces.py` with this content:

```python
"""Workspace locations CRUD API."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.db.engine import get_db
from app.db.models import User, WorkspaceLocation
from app.web.deps import require_user

router = APIRouter(prefix="/api/workspaces")


class LocationCreate(BaseModel):
    path: str
    label: str
    writable: bool = True


class LocationUpdate(BaseModel):
    label: str | None = None
    writable: bool | None = None


def _resolve_path(path_str: str) -> Path:
    p = Path(path_str.strip()).resolve()
    return p


@router.get("")
async def list_workspaces(
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(WorkspaceLocation).order_by(
                WorkspaceLocation.is_primary.desc(),
                WorkspaceLocation.created_at,
            )
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "path": r.path,
            "label": r.label,
            "is_primary": r.is_primary,
            "writable": r.writable,
        }
        for r in rows
    ]


@router.post("")
async def add_workspace(
    body: LocationCreate,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    new_path = _resolve_path(body.path)
    if not new_path.exists():
        try:
            new_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot create directory: {e}")
    if not new_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory.")

    resolved_str = str(os.path.realpath(str(new_path)))

    existing = (
        await db.execute(
            select(WorkspaceLocation).where(WorkspaceLocation.path == resolved_str)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="This path is already a workspace location.")

    loc = WorkspaceLocation(
        path=resolved_str,
        label=body.label.strip() or new_path.name,
        is_primary=False,
        writable=body.writable,
    )
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return {"id": loc.id, "path": loc.path, "label": loc.label, "is_primary": False, "writable": loc.writable}


@router.patch("/{loc_id}")
async def update_workspace(
    loc_id: int,
    body: LocationUpdate,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    loc = (
        await db.execute(select(WorkspaceLocation).where(WorkspaceLocation.id == loc_id))
    ).scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Workspace location not found.")
    if body.label is not None:
        loc.label = body.label.strip()
    if body.writable is not None:
        loc.writable = body.writable
    await db.commit()
    return {"ok": True}


@router.delete("/{loc_id}")
async def delete_workspace(
    loc_id: int,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    loc = (
        await db.execute(select(WorkspaceLocation).where(WorkspaceLocation.id == loc_id))
    ).scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Workspace location not found.")
    if loc.is_primary:
        raise HTTPException(status_code=400, detail="Cannot delete the primary workspace. Set another location as primary first.")
    await db.delete(loc)
    await db.commit()
    return {"ok": True}


@router.post("/{loc_id}/set-primary")
async def set_primary_workspace(
    loc_id: int,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    loc = (
        await db.execute(select(WorkspaceLocation).where(WorkspaceLocation.id == loc_id))
    ).scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Workspace location not found.")

    # Demote current primary
    current_primary = (
        await db.execute(
            select(WorkspaceLocation).where(WorkspaceLocation.is_primary == True)  # noqa: E712
        )
    ).scalar_one_or_none()
    if current_primary:
        current_primary.is_primary = False

    loc.is_primary = True
    await db.commit()

    # Update live config + .env
    new_path = Path(loc.path)
    app_config.WORKSPACE_DIR = new_path

    env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        found = False
        new_lines = []
        for line in lines:
            if line.startswith("WORKSPACE_DIR="):
                new_lines.append(f"WORKSPACE_DIR={new_path}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"WORKSPACE_DIR={new_path}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return {"ok": True, "path": str(new_path)}
```

- [ ] **Step 2: Register router in main.py**

In `app/main.py`, add after the other router imports:
```python
from app.web.routes.workspaces import router as workspaces_router
```

And in the `app.include_router(...)` block, add:
```python
app.include_router(workspaces_router)
```

- [ ] **Step 3: Verify routes load cleanly**

```bash
cd "E:/BTP project" && python -c "from app.web.routes.workspaces import router; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git -C "E:/BTP project" add app/web/routes/workspaces.py app/main.py
git -C "E:/BTP project" commit -m "feat: workspace locations CRUD API"
```

---

## Task 6: Update settings route to pass workspace data to template

**Files:**
- Modify: `app/web/routes/settings.py`

- [ ] **Step 1: Add WorkspaceLocation import**

In `app/web/routes/settings.py`, find:
```python
from app.db.models import Message, OAuthToken, Thread, User
```
Change to:
```python
from app.db.models import Message, OAuthToken, Thread, User, WorkspaceLocation
```

- [ ] **Step 2: Update the settings_page handler to pass secondary workspaces**

Find the `settings_page` function. Replace the `return templates.TemplateResponse(...)` call with:

```python
    # Load secondary workspace locations for the template
    from sqlalchemy import select as _select
    secondary_workspaces = []
    async with AsyncSessionLocal() as _db:
        _rows = (await _db.execute(
            _select(WorkspaceLocation)
            .where(WorkspaceLocation.is_primary == False)  # noqa: E712
            .order_by(WorkspaceLocation.created_at)
        )).scalars().all()
        secondary_workspaces = [
            {"id": r.id, "path": r.path, "label": r.label, "writable": r.writable}
            for r in _rows
        ]

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "google_connected": google_connected,
            "google_configured": google_configured,
            "fernet_configured": fernet_configured,
            "workspace_dir": str(app_config.WORKSPACE_DIR),
            "secondary_workspaces": secondary_workspaces,
            "available_models": list(AVAILABLE_MODELS),
            "default_thread_model": app_config.DEFAULT_THREAD_MODEL,
            "telegram_bot_token": app_config.TELEGRAM_BOT_TOKEN,
            "telegram_chat_id": app_config.TELEGRAM_CHAT_ID,
            "telegram_webhook_url": app_config.TELEGRAM_WEBHOOK_URL,
            "telegram_webhook_active": bool(app_config.TELEGRAM_WEBHOOK_URL and app_config.TELEGRAM_WEBHOOK_SECRET),
        },
    )
```

- [ ] **Step 3: Update `update_workspace` to also upsert the DB primary row**

Find the `update_workspace` function. After the line `app_config.WORKSPACE_DIR = new_path`, add a DB upsert:

```python
    # Update live config value immediately
    app_config.WORKSPACE_DIR = new_path

    # Upsert the primary WorkspaceLocation row in the DB
    from sqlalchemy import select as _select
    resolved_str = str(new_path)
    async with AsyncSessionLocal() as _db:
        current_primary = (await _db.execute(
            _select(WorkspaceLocation).where(WorkspaceLocation.is_primary == True)  # noqa: E712
        )).scalar_one_or_none()
        if current_primary:
            current_primary.path = resolved_str
            current_primary.label = "Main workspace"
        else:
            _db.add(WorkspaceLocation(
                path=resolved_str, label="Main workspace",
                is_primary=True, writable=True,
            ))
        await _db.commit()
```

- [ ] **Step 4: Verify settings route imports cleanly**

```bash
cd "E:/BTP project" && python -c "from app.web.routes.settings import router; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git -C "E:/BTP project" add app/web/routes/settings.py
git -C "E:/BTP project" commit -m "feat: pass secondary workspaces to settings template"
```

---

## Task 7: Settings UI — additional locations disclosure section

**Files:**
- Modify: `app/web/templates/settings.html`

- [ ] **Step 1: Add flash banners for workspace location actions**

In `settings.html`, find the flash banners block (near the top of `<main>`). Add these after the existing `ws_ok`/`ws_error` banners:

```html
    {% set loc_ok     = request.query_params.get('loc_ok') %}
    {% set loc_error  = request.query_params.get('loc_error') %}
    {% if loc_ok %}
    <div class="rounded-xl bg-green-50 border border-green-200 px-4 py-3 text-sm text-green-800">
      Workspace locations updated.
    </div>
    {% elif loc_error %}
    <div class="rounded-xl bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
      {{ loc_error }}
    </div>
    {% endif %}
```

- [ ] **Step 2: Replace the workspace section in settings.html**

Find the entire `<!-- Workspace Directory Card -->` section (from `<section class="bg-white rounded-2xl...">` through the closing `</section>`). Replace it with:

```html
    <!-- Workspace Directory Card -->
    <section class="bg-white rounded-2xl border border-gray-200 shadow-sm overflow-hidden">
      <div class="px-6 py-4 border-b bg-gray-50 flex items-center gap-3">
        <svg class="w-4 h-4 text-gray-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>
        </svg>
        <h2 class="text-sm font-semibold text-gray-700">Workspace Directory</h2>
      </div>
      <div class="px-6 py-5 space-y-4">
        <!-- Primary workspace -->
        <form action="/settings/workspace" method="post" class="space-y-3">
          <p class="text-xs text-gray-500">
            The assistant reads and writes files within this directory by default.
            Changes take effect immediately and are saved to <code class="font-mono bg-gray-100 rounded px-1">.env</code>.
          </p>
          <input type="text" name="workspace" value="{{ workspace_dir }}"
                 class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm font-mono
                        focus:outline-none focus:ring-2 focus:ring-blue-500"/>
          <button type="submit"
                  class="px-4 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium
                         hover:bg-blue-700 transition-colors">
            Save workspace
          </button>
        </form>

        <!-- Additional locations disclosure -->
        <details id="extra-workspaces" class="border border-gray-200 rounded-lg">
          <summary class="flex items-center justify-between px-4 py-3 cursor-pointer select-none text-sm text-gray-700 font-medium hover:bg-gray-50 rounded-lg">
            <span>Additional locations
              {% if secondary_workspaces %}
              <span class="ml-1.5 text-xs font-normal text-gray-400">({{ secondary_workspaces|length }})</span>
              {% endif %}
            </span>
            <svg class="w-4 h-4 text-gray-400 transition-transform details-chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
            </svg>
          </summary>
          <div class="px-4 pb-4 pt-2 space-y-2" id="locations-list">
            {% if secondary_workspaces %}
            {% for loc in secondary_workspaces %}
            <div class="flex items-center gap-2 py-2 border-b border-gray-100 last:border-0" id="loc-row-{{ loc.id }}">
              <div class="flex-1 min-w-0">
                <p class="text-xs font-medium text-gray-700 truncate">{{ loc.label }}</p>
                <p class="text-xs text-gray-400 font-mono truncate">{{ loc.path }}</p>
              </div>
              <span class="shrink-0 text-xs px-2 py-0.5 rounded-full border
                {% if loc.writable %}bg-green-50 border-green-200 text-green-700{% else %}bg-gray-100 border-gray-200 text-gray-500{% endif %}">
                {{ 'read+write' if loc.writable else 'read-only' }}
              </span>
              <button type="button"
                      onclick="setLocPrimary({{ loc.id }})"
                      class="shrink-0 text-xs px-2 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50 transition-colors">
                Set primary
              </button>
              <button type="button"
                      onclick="deleteLoc({{ loc.id }})"
                      class="shrink-0 text-gray-400 hover:text-red-500 transition-colors">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                </svg>
              </button>
            </div>
            {% endfor %}
            {% else %}
            <p class="text-xs text-gray-400 py-1">No additional locations added yet.</p>
            {% endif %}

            <!-- Add location inline form -->
            <div id="add-loc-form" class="hidden pt-2 space-y-2">
              <div class="flex gap-2">
                <input id="new-loc-label" type="text" placeholder="Label (e.g. E Drive)"
                       class="w-1/3 rounded border border-gray-300 px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"/>
                <input id="new-loc-path" type="text" placeholder="Path (e.g. E:\Projects)"
                       class="flex-1 rounded border border-gray-300 px-2 py-1.5 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"/>
              </div>
              <div class="flex items-center gap-3">
                <label class="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
                  <input type="checkbox" id="new-loc-writable" checked class="rounded"/>
                  Read+write
                </label>
                <button type="button" onclick="submitAddLoc()"
                        class="px-3 py-1 rounded bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors">
                  Add
                </button>
                <button type="button" onclick="cancelAddLoc()"
                        class="px-3 py-1 rounded border border-gray-300 text-gray-600 text-xs hover:bg-gray-50 transition-colors">
                  Cancel
                </button>
                <span id="add-loc-error" class="text-xs text-red-500 hidden"></span>
              </div>
            </div>

            <div class="pt-1">
              <button type="button" id="show-add-loc-btn" onclick="showAddLoc()"
                      class="text-xs text-blue-600 hover:text-blue-800 font-medium transition-colors">
                + Add location
              </button>
            </div>
          </div>
        </details>
      </div>
    </section>
```

- [ ] **Step 3: Add the JS for the locations UI**

Before the closing `</body>` tag in `settings.html` (or at the end of any existing `<script>` block), add:

```html
<script>
function showAddLoc() {
  document.getElementById('add-loc-form').classList.remove('hidden');
  document.getElementById('show-add-loc-btn').classList.add('hidden');
  document.getElementById('new-loc-label').focus();
}
function cancelAddLoc() {
  document.getElementById('add-loc-form').classList.add('hidden');
  document.getElementById('show-add-loc-btn').classList.remove('hidden');
  document.getElementById('add-loc-error').classList.add('hidden');
  document.getElementById('new-loc-label').value = '';
  document.getElementById('new-loc-path').value = '';
  document.getElementById('new-loc-writable').checked = true;
}
async function submitAddLoc() {
  const label = document.getElementById('new-loc-label').value.trim();
  const path  = document.getElementById('new-loc-path').value.trim();
  const writable = document.getElementById('new-loc-writable').checked;
  const errEl = document.getElementById('add-loc-error');
  errEl.classList.add('hidden');
  if (!path) { errEl.textContent = 'Path is required.'; errEl.classList.remove('hidden'); return; }
  const resp = await fetch('/api/workspaces', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label: label || path, path, writable }),
  });
  if (resp.ok) {
    location.reload();
  } else {
    const data = await resp.json().catch(() => ({}));
    errEl.textContent = data.detail || 'Failed to add location.';
    errEl.classList.remove('hidden');
  }
}
async function deleteLoc(id) {
  const resp = await fetch(`/api/workspaces/${id}`, { method: 'DELETE' });
  if (resp.ok) {
    document.getElementById(`loc-row-${id}`)?.remove();
  } else {
    const data = await resp.json().catch(() => ({}));
    alert(data.detail || 'Could not remove location.');
  }
}
async function setLocPrimary(id) {
  const resp = await fetch(`/api/workspaces/${id}/set-primary`, { method: 'POST' });
  if (resp.ok) {
    location.reload();
  } else {
    const data = await resp.json().catch(() => ({}));
    alert(data.detail || 'Could not set as primary.');
  }
}
// Rotate chevron when details is opened/closed
document.addEventListener('DOMContentLoaded', () => {
  const det = document.getElementById('extra-workspaces');
  if (det) {
    det.addEventListener('toggle', () => {
      det.querySelector('.details-chevron').style.transform = det.open ? 'rotate(180deg)' : '';
    });
  }
});
</script>
```

- [ ] **Step 4: Verify the app starts and the settings page loads**

Start the app:
```bash
cd "E:/BTP project" && python run.py
```

Open `http://localhost:8000/settings` in the browser. You should see:
- Primary workspace input + Save button (unchanged)
- A collapsed "Additional locations" disclosure below it
- Clicking it expands to show "No additional locations added yet." + "+ Add location" button
- Clicking "+ Add location" shows the inline form

- [ ] **Step 5: Commit**

```bash
git -C "E:/BTP project" add app/web/templates/settings.html
git -C "E:/BTP project" commit -m "feat: additional workspace locations UI in settings"
```

---

## Task 8: Dark theme overrides for new UI elements

**Files:**
- Modify: `app/web/templates/base.html`

- [ ] **Step 1: Add dark mode overrides for the disclosure section**

In `app/web/templates/base.html`, find the `/* Examples panel in connectors */` section in the `<style>` block. Add these overrides after it:

```css
    /* Additional workspace locations disclosure */
    [data-theme="dark"] #extra-workspaces                           { border-color: #334155 !important; }
    [data-theme="dark"] #extra-workspaces summary                   { color: #e2e8f0 !important; }
    [data-theme="dark"] #extra-workspaces summary:hover             { background-color: #1e293b !important; }
    [data-theme="dark"] #extra-workspaces #locations-list           { background-color: #1e293b !important; }
    [data-theme="dark"] #extra-workspaces .border-gray-100          { border-color: #334155 !important; }
    [data-theme="dark"] #extra-workspaces .text-gray-400            { color: #94a3b8 !important; }
    [data-theme="dark"] #extra-workspaces .border-gray-300          { border-color: #475569 !important; }
    [data-theme="dark"] #extra-workspaces .text-gray-600            { color: #cbd5e1 !important; }
```

- [ ] **Step 2: Verify dark mode looks correct**

Toggle dark mode on the settings page and confirm the disclosure section renders cleanly (no white boxes, readable text).

- [ ] **Step 3: Commit**

```bash
git -C "E:/BTP project" add app/web/templates/base.html
git -C "E:/BTP project" commit -m "feat: dark theme for workspace locations disclosure"
```

---

## Self-Review Notes

**Spec coverage check:**
- ✅ `WorkspaceLocation` DB table → Task 1
- ✅ `app_config.WORKSPACE_DIR` stays as runtime primary → Tasks 2 + 6
- ✅ Seed on startup → Task 2
- ✅ `_safe_resolve()` expanded to multi-root + `require_writable` → Task 3
- ✅ `find_file` tool → Task 3
- ✅ Agent gets `find_file` → Task 4
- ✅ CRUD API routes → Task 5
- ✅ Settings template receives secondary workspaces → Task 6
- ✅ Settings UI disclosure section → Task 7
- ✅ Dark theme → Task 8
- ✅ Migration via seed → Task 2

**Type consistency:**
- `_get_allowed_roots()` returns `list[tuple[Path, bool]]` — used correctly in `_safe_resolve` and `find_file`
- `WorkspaceLocation` fields match across models.py, seed.py, routes/workspaces.py, and template
- `secondary_workspaces` is a list of dicts `{id, path, label, writable}` — used consistently in template
