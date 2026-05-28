"""Append-only episodic run log per agent: agents/<id>_episodes.jsonl.

One JSON object per agent run. Never rewritten, never distilled — the durable
audit trail that powers the agent_recall tool and the UI activity timeline.
All writes are best-effort: a logging failure must never fail an agent run.
"""
from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _append_raw(path: Path, obj: dict) -> None:
    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def append_episode(path: Path, run_id: int, trigger_type: str,
                   summary: str, actions: list[str]) -> None:
    """Append one run's episode. Best-effort — never raises."""
    _append_raw(path, {
        "run_id": run_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "trigger_type": trigger_type or "",
        "summary": (summary or "")[:500],
        "actions": actions or [],
    })


def read_episodes(path: Path) -> list[dict]:
    """Return all episodes (oldest first). [] if the file is missing/unreadable."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with contextlib.suppress(Exception):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(Exception):
                rows.append(json.loads(line))
    return rows


def recall(path: Path, query: str, since: str | None = None) -> list[dict]:
    """Keyword (substring, case-insensitive) match over summary+actions, with an
    optional ISO date floor on `ts`. Empty query matches all (subject to `since`)."""
    q = (query or "").lower().strip()
    floor: datetime | None = None
    if since:
        with contextlib.suppress(Exception):
            floor = datetime.fromisoformat(since)
            if floor.tzinfo is None:
                floor = floor.replace(tzinfo=timezone.utc)
    out: list[dict] = []
    for r in read_episodes(path):
        if floor is not None:
            with contextlib.suppress(Exception):
                ts = datetime.fromisoformat(r.get("ts", ""))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < floor:
                    continue
        if q:
            hay = (r.get("summary", "") + " " + " ".join(r.get("actions", []))).lower()
            if q not in hay:
                continue
        out.append(r)
    return out
