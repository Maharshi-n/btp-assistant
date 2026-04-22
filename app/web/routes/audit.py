"""Phase 10: Permission audit log viewer at /audit."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import PermissionAudit, User
from app.web.deps import require_user

BASE_DIR = Path(__file__).resolve().parent.parent.parent
router = APIRouter(prefix="/audit")
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

PAGE_SIZE = 20


@router.get("")
async def audit_page(
    request: Request,
    page: int = 1,
    _user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    offset = (max(page, 1) - 1) * PAGE_SIZE

    total_result = await db.execute(select(func.count()).select_from(PermissionAudit))
    total: int = total_result.scalar_one()

    rows_result = await db.execute(
        select(PermissionAudit)
        .order_by(PermissionAudit.decided_at.desc())
        .offset(offset)
        .limit(PAGE_SIZE)
    )
    rows = rows_result.scalars().all()

    # Truncate args for display
    def truncate_args(args_json: str, max_len: int = 120) -> str:
        try:
            d = json.loads(args_json)
            text = json.dumps(d, ensure_ascii=False)
        except Exception:
            text = args_json
        if len(text) > max_len:
            return text[:max_len] + "…"
        return text

    entries = [
        {
            "id": r.id,
            "tool_name": r.tool_name,
            "args_short": truncate_args(r.args_json),
            "decision": r.decision,
            "decided_by": r.decided_by,
            "decided_at": r.decided_at.strftime("%Y-%m-%d %H:%M:%S") if r.decided_at else "—",
            "thread_id": r.thread_id,
        }
        for r in rows
    ]

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return templates.TemplateResponse(
        "audit.html",
        {
            "request": request,
            "entries": entries,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )
