"""Scheduled Tasks routes.

Endpoints:
  GET  /api/tasks              → list tasks
  POST /api/tasks              → create (NL description)
  POST /api/tasks/{id}/enable  → enable
  POST /api/tasks/{id}/disable → disable
  DELETE /api/tasks/{id}       → delete
  GET  /api/tasks/{id}/runs    → recent runs
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.parser import parse_automation
from app.db.engine import get_db
from app.db.models import ScheduledTask, ScheduledTaskRun, User
from app.web.deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Runtime helpers
# ---------------------------------------------------------------------------

def _register_task(task: ScheduledTask) -> None:
    from app.automations.runtime import get_scheduler
    from apscheduler.triggers.cron import CronTrigger
    scheduler = get_scheduler()
    if scheduler is None or not scheduler.running:
        return
    job_id = f"task_{task.id}"
    scheduler.add_job(
        _fire_task,
        CronTrigger.from_crontab(task.cron),
        id=job_id,
        args=[task.id],
        replace_existing=True,
        max_instances=1,
    )
    logger.info("Registered scheduled task %d: %s", task.id, task.cron)


def _unregister_task(task_id: int) -> None:
    from app.automations.runtime import get_scheduler
    scheduler = get_scheduler()
    if scheduler is None:
        return
    job_id = f"task_{task_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


async def _fire_task(task_id: int) -> None:
    """Run the scheduled task through the supervisor."""
    import datetime
    from app.db.engine import AsyncSessionLocal
    from app.db.models import Message, Thread

    async with AsyncSessionLocal() as db:
        task = await db.get(ScheduledTask, task_id)
        if not task or not task.enabled:
            return

        thread = Thread(title=f"[Task] {task.name[:60]}", model="gpt-4o-mini")
        db.add(thread)
        await db.flush()

        _EXEC_PREFIX = (
            "[SCHEDULED TASK — execute immediately. Call tools directly as instructed. "
            "You MAY call telegram_ask or telegram_send to notify the user.]\n\n"
        )
        effective_prompt = _EXEC_PREFIX + task.action_prompt

        msg = Message(thread_id=thread.id, role="user", content=effective_prompt)
        db.add(msg)

        run = ScheduledTaskRun(task_id=task.id, thread_id=thread.id, status="running")
        db.add(run)
        task.last_run_at = datetime.datetime.now(datetime.timezone.utc)
        await db.commit()
        await db.refresh(run)
        run_id = run.id
        thread_id = thread.id

    # Run supervisor via LangGraph (same as automations)
    status = "failed"
    try:
        from langchain_core.messages import HumanMessage
        from app.agents.supervisor import get_graph
        graph = get_graph()
        lg_config = {"recursion_limit": 100, "configurable": {"thread_id": str(thread_id), "model": "gpt-4o-mini"}}
        await graph.ainvoke({"messages": [HumanMessage(content=effective_prompt)]}, lg_config)
        status = "done"
    except Exception as exc:
        logger.warning("Scheduled task %d failed: %s", task_id, exc)

    async with AsyncSessionLocal() as db:
        run = await db.get(ScheduledTaskRun, run_id)
        if run:
            import datetime as dt
            run.status = status
            run.finished_at = dt.datetime.now(dt.timezone.utc)
            await db.commit()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/api/tasks")
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(select(ScheduledTask).order_by(ScheduledTask.created_at.desc()))
    tasks = result.scalars().all()
    out = []
    for t in tasks:
        last_run_result = await db.execute(
            select(ScheduledTaskRun)
            .where(ScheduledTaskRun.task_id == t.id)
            .order_by(ScheduledTaskRun.started_at.desc())
            .limit(1)
        )
        last_run = last_run_result.scalars().first()
        out.append({
            "id": t.id,
            "name": t.name,
            "cron": t.cron,
            "action_prompt": t.action_prompt,
            "enabled": t.enabled,
            "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
            "created_at": t.created_at.isoformat(),
            "last_run": {
                "status": last_run.status,
                "thread_id": last_run.thread_id,
                "started_at": last_run.started_at.isoformat(),
            } if last_run else None,
        })
    return out


@router.post("/api/tasks")
async def create_task(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    nl: str = payload.get("nl_description", "").strip()
    if not nl:
        raise HTTPException(422, "nl_description is required")

    try:
        parsed = await parse_automation(nl, db=db)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("Error parsing task: %s", exc)
        raise HTTPException(500, "Failed to parse task description")

    if parsed["trigger_type"] != "cron":
        raise HTTPException(400, "Scheduled tasks only support cron triggers. Describe a recurring schedule (e.g. 'every monday at 9am').")

    cron = parsed["trigger_config"].get("cron", "")
    if not cron:
        raise HTTPException(400, "Could not extract cron expression from description.")

    task = ScheduledTask(
        name=parsed["name"],
        cron=cron,
        action_prompt=parsed["action_prompt"],
        enabled=True,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    _register_task(task)

    return {
        "id": task.id,
        "name": task.name,
        "cron": task.cron,
        "action_prompt": task.action_prompt,
        "enabled": task.enabled,
        "created_at": task.created_at.isoformat(),
    }


@router.post("/api/tasks/{task_id}/enable")
async def enable_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    task = await db.get(ScheduledTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.enabled = True
    await db.commit()
    _register_task(task)
    return {"id": task_id, "enabled": True}


@router.post("/api/tasks/{task_id}/disable")
async def disable_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    task = await db.get(ScheduledTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.enabled = False
    await db.commit()
    _unregister_task(task_id)
    return {"id": task_id, "enabled": False}


@router.delete("/api/tasks/{task_id}")
async def delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    task = await db.get(ScheduledTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _unregister_task(task_id)
    await db.execute(delete(ScheduledTaskRun).where(ScheduledTaskRun.task_id == task_id))
    await db.delete(task)
    await db.commit()
    return {"deleted": True}


@router.get("/api/tasks/{task_id}/runs")
async def get_task_runs(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    task = await db.get(ScheduledTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    result = await db.execute(
        select(ScheduledTaskRun)
        .where(ScheduledTaskRun.task_id == task_id)
        .order_by(ScheduledTaskRun.started_at.desc())
        .limit(20)
    )
    runs = result.scalars().all()
    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "status": r.status,
            "thread_id": r.thread_id,
        }
        for r in runs
    ]
