"""Phase 5: workspace-aware filesystem tools.

All tools resolve the given path and check it is inside WORKSPACE_DIR.
Paths outside the workspace raise OutsideWorkspaceError.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

import app.config as app_config


class OutsideWorkspaceError(ValueError):
    """Raised when a path falls outside the allowed workspace directory."""


def _safe_resolve(path_str: str) -> Path:
    """Resolve *path_str* relative to WORKSPACE_DIR and verify it stays inside.

    Raises OutsideWorkspaceError if the resolved path escapes the workspace.
    """
    workspace = app_config.WORKSPACE_DIR
    # Treat relative paths as relative to workspace
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    # Ensure it's inside (or equal to) the workspace
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise OutsideWorkspaceError(
            f"Path '{path_str}' is outside the allowed workspace '{workspace}'. "
            "I can only access files inside the workspace directory."
        )
    return resolved


@tool
def read_file(path: Annotated[str, "Path to the file to read (relative to workspace or absolute)"]) -> str:
    """Read the contents of a file inside the workspace directory."""
    try:
        resolved = _safe_resolve(path)
    except OutsideWorkspaceError as e:
        return str(e)

    if not resolved.exists():
        return f"Error: File '{path}' does not exist."
    if not resolved.is_file():
        return f"Error: '{path}' is not a file."

    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error reading file: {e}"


@tool
def write_file(
    path: Annotated[str, "Path to write (relative to workspace or absolute)"],
    content: Annotated[str, "Content to write to the file"],
) -> str:
    """Write content to a file inside the workspace directory. Creates the file if it does not exist."""
    try:
        resolved = _safe_resolve(path)
    except OutsideWorkspaceError as e:
        return str(e)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} characters to '{path}'."
    except OSError as e:
        return f"Error writing file: {e}"


@tool
def list_dir(path: Annotated[str, "Directory path to list (relative to workspace or absolute, default '.')"] = ".") -> str:
    """List the files and directories inside a workspace directory."""
    try:
        resolved = _safe_resolve(path)
    except OutsideWorkspaceError as e:
        return str(e)

    if not resolved.exists():
        return f"Error: Directory '{path}' does not exist."
    if not resolved.is_dir():
        return f"Error: '{path}' is not a directory."

    try:
        entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = []
        for entry in entries:
            kind = "FILE" if entry.is_file() else "DIR "
            size = ""
            if entry.is_file():
                try:
                    size = f"  ({entry.stat().st_size} bytes)"
                except OSError:
                    pass
            lines.append(f"[{kind}] {entry.name}{size}")
        if not lines:
            return f"Directory '{path}' is empty."
        return "\n".join(lines)
    except OSError as e:
        return f"Error listing directory: {e}"


@tool
def delete_file(path: Annotated[str, "Path to the file to delete (relative to workspace or absolute)"]) -> str:
    """Delete a file inside the workspace directory. Directories are not deleted by this tool."""
    try:
        resolved = _safe_resolve(path)
    except OutsideWorkspaceError as e:
        return str(e)

    if not resolved.exists():
        return f"Error: File '{path}' does not exist."
    if not resolved.is_file():
        return f"Error: '{path}' is a directory, not a file. This tool only deletes files."

    try:
        resolved.unlink()
        return f"Successfully deleted '{path}'."
    except OSError as e:
        return f"Error deleting file: {e}"
