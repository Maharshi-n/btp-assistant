"""Phase 6: permission approval/denial endpoint.

POST /api/permissions/:id  {"decision": "approved"|"denied"}

  1. Looks up the pending interrupt by request_id.
  2. Logs the decision to permission_audit.
  3. Resumes the LangGraph graph with Command(resume={"decision": ...}).
  4. Streams the resumed run in a background task (same as a normal message).
  5. Pushes permission_resolved over WebSocket so the UI can remove the card.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.agents.supervisor import get_graph
from app.db.engine import AsyncSessionLocal
from app.db.models import PermissionAudit, User
from app.web.deps import require_user
from app.web.routes.chat import _stream_langgraph, get_pending_permissions
from app.web.routes.ws import manager as ws_manager
from langgraph.types import Command

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.post("/permissions/{request_id}")
async def decide_permission(
    request_id: str,
    payload: dict,
    background_tasks: BackgroundTasks,
    _user: User = Depends(require_user),
):
    pending = get_pending_permissions()
    req = pending.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Permission request not found or already resolved")

    decision: str = payload.get("decision", "denied")
    if decision not in ("approved", "denied"):
        raise HTTPException(status_code=422, detail="decision must be 'approved' or 'denied'")

    thread_id: int = req["thread_id"]
    tool_name: str = req["tool"]
    tool_args: dict = req["args"]
    lg_config: dict = req["lg_config"]
    model: str = req["model"]

    # Remove from pending so it can't be double-submitted
    del pending[request_id]

    # Log to permission_audit
    await _log_user_decision(
        request_id=request_id,
        tool_name=tool_name,
        tool_args=tool_args,
        decision=decision,
        thread_id=thread_id,
    )

    # Notify UI that the card is resolved
    await ws_manager.send(thread_id, {
        "type": "permission_resolved",
        "id": request_id,
        "decision": decision,
    })

    # Resume the graph — pass the decision back to interrupt()
    resume_cmd = Command(resume={"decision": decision, "request_id": request_id})
    background_tasks.add_task(_stream_langgraph, thread_id, model, resume_cmd)

    return {"status": "ok", "decision": decision}


async def _log_user_decision(
    *,
    request_id: str,
    tool_name: str,
    tool_args: dict,
    decision: str,
    thread_id: int,
) -> None:
    """Write the user's approve/deny to permission_audit.  Best-effort."""
    try:
        async with AsyncSessionLocal() as db:
            row = PermissionAudit(
                tool_name=tool_name,
                args_json=json.dumps(tool_args, default=str),
                decision=decision,
                decided_by="user",
                decided_at=datetime.now(timezone.utc),
                thread_id=thread_id,
                request_id=request_id,
            )
            db.add(row)
            await db.commit()
    except Exception:
        logger.exception("Failed to log permission audit for request %s", request_id)
