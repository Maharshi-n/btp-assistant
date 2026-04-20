# Multi-Workspace Support — Design Spec
**Date:** 2026-04-20  
**Project:** RAION  
**Status:** Approved

---

## Overview

Replace the single `WORKSPACE_DIR` with a primary workspace + N secondary locations. The agent saves to the primary by default, searches primary-first then secondary locations, and respects per-location write permissions.

---

## Data Layer

### New table: `WorkspaceLocation`

```python
class WorkspaceLocation(Base):
    __tablename__ = "workspace_locations"

    id:         int   (primary key)
    path:       str   (absolute, unique)
    label:      str   (display name, e.g. "E Drive Projects")
    is_primary: bool  (exactly one row is True at any time)
    writable:   bool  (True = agent can write here; False = read/search only)
    created_at: datetime
```

**Invariants:**
- Exactly one row has `is_primary = True` at all times. Enforced in the route layer (not DB constraint) by flipping the old primary to False before setting the new one.
- `path` is stored as an absolute, resolved string.

### `app_config.WORKSPACE_DIR`

Stays as the runtime source-of-truth for the primary workspace. On startup (`init_db` / app factory), the primary row is read from the DB and `app_config.WORKSPACE_DIR` is set to it. When the user changes the primary via the settings UI, both the DB row and `app_config.WORKSPACE_DIR` are updated atomically.

Secondary locations are **not** cached in config — they are loaded from the DB per tool call (single fast query, no meaningful overhead).

---

## `filesystem.py` Changes

### `_safe_resolve()` — expanded allowlist

Current behaviour: path must be inside `app_config.WORKSPACE_DIR`.  
New behaviour: path must be inside the primary OR any secondary location.

```python
def _get_allowed_roots() -> list[tuple[Path, bool]]:
    """Return [(resolved_path, writable), ...] for all workspace locations."""
    with SyncSessionLocal() as db:
        rows = db.execute(sa_select(WorkspaceLocation)).scalars().all()
    return [(Path(os.path.realpath(r.path)), r.writable) for r in rows]
```

`_safe_resolve(path_str, require_writable=False)`:
1. Resolve candidate path as before.
2. Check candidate against each allowed root.
3. If `require_writable=True` and the matching root has `writable=False`, raise `OutsideWorkspaceError("This location is read-only.")`.
4. If no root matches, raise `OutsideWorkspaceError` as today.

`write_file`, `create_folder`, `delete_file`, `copy_file`, `move_file` all pass `require_writable=True`.  
`read_file`, `list_dir` pass `require_writable=False`.

### New tool: `find_file`

```python
@tool
def find_file(filename: str) -> str:
    """Search for a file by name across all workspace locations.
    Searches the primary workspace first, then secondary locations in order added.
    Returns the full path if found, or a not-found message."""
```

Algorithm:
1. Load all locations (primary first, then secondaries ordered by `created_at`).
2. `os.walk()` each root, match by filename (case-insensitive on Windows).
3. Return first match with its full path and which workspace it was found in.
4. If nothing found after all locations: return clear not-found message.

For very large drives the walk is synchronous but runs inside a LangChain tool call (already off the main async loop via LangGraph's executor), so no special threading needed.

---

## Settings UI

The workspace section in `settings.html` gets a minimal refresh:

```
Primary Workspace
[ /path/to/primary/workspace          ] [Save]

▼ Additional locations (2)                    [+ Add]
  ┌────────────────────────────────────────────────┐
  │ E:\Projects      read-only    [Set primary] [✕]│
  │ D:\Downloads     read+write   [Set primary] [✕]│
  └────────────────────────────────────────────────┘
```

- The "Additional locations" block is a `<details>`/`<summary>` disclosure element, collapsed by default.
- Summary line shows the count: "Additional locations (N)".
- Each row: label (editable inline or on add), path, writable badge (toggle), "Set primary" button, remove button.
- "Add" opens an inline form row: `[label input] [path input] [○ read-only  ● read+write] [Save] [Cancel]`.
- All actions use HTMX partial reloads (same pattern as connectors page).

---

## New API Routes (`/api/workspaces`)

| Method | Path | Action |
|--------|------|--------|
| GET | `/api/workspaces` | List all locations |
| POST | `/api/workspaces` | Add a secondary location |
| PATCH | `/api/workspaces/{id}` | Toggle writable / update label |
| DELETE | `/api/workspaces/{id}` | Remove a secondary location |
| POST | `/api/workspaces/{id}/set-primary` | Promote to primary |

Primary workspace update reuses the existing `POST /workspace` route — it now also upserts the DB row.

---

## Agent Behaviour Summary

| Scenario | Behaviour |
|----------|-----------|
| "Save this file" (no path) | Write to primary workspace |
| "Find my resume PDF" | `find_file` — primary first, then secondaries |
| Explicit path given | Resolved against whichever location it falls under |
| Write to a read-only location | Blocked with clear error message |
| Path outside all locations | Blocked with `OutsideWorkspaceError` |

---

## Migration

On first startup after this change, the existing `WORKSPACE_DIR` value is seeded as the primary `WorkspaceLocation` row (in `db/seed.py`). No data migration needed for existing threads/messages.

---

## Out of Scope

- Per-location file-type restrictions
- Quota / size limits per location
- Network paths (UNC `\\server\share`) — already blocked by existing path safety checks
