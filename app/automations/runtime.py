"""Phase 9: Automations runtime.

Responsibilities:
- On startup: load all enabled automations from DB and register them.
- cron automations        → APScheduler CronTrigger job.
- gmail_new_from_sender   → APScheduler IntervalTrigger job (every 2 min),
                            tracks last_seen_message_id per sender.
- fs_new_in_folder        → watchdog FileSystemEventHandler per folder.
- When a trigger fires    → create a new Thread + first Message, run the
                            action_prompt through the LangGraph supervisor
                            (exactly like a user message), log an AutomationRun.
- On disable/delete       → remove the job / observer immediately.
- On startup after restart → all automations reload from DB.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer

import app.config as app_config
from app.db.engine import AsyncSessionLocal
from app.db.models import Automation, AutomationRun, Message, Thread

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_scheduler: AsyncIOScheduler | None = None
_observer: Observer | None = None

# Maps automation_id → watchdog handler reference (so we can remove them)
_fs_handlers: dict[int, tuple[Any, str]] = {}  # id → (handler, watch_path)

# Per-automation lock — prevents concurrent poll coroutines from double-firing
_gmail_poll_locks: dict[int, asyncio.Lock] = {}

# WhatsApp polling background task handle
_wa_poll_task: asyncio.Task | None = None

# Whether polling is active — can be toggled at runtime without restarting
_wa_polling_enabled: bool = True

# Unix timestamp of when the server started — messages older than this are
# skipped on the first poll after a restart to avoid replaying the backlog
_server_start_unix: float = 0.0

# Guard to prevent start_automations_runtime from running more than once
_runtime_started: bool = False

# ---------------------------------------------------------------------------
# WhatsApp debounce — 15-second window per (chat_id, sender_id)
# ---------------------------------------------------------------------------

# chat_id → asyncio.TimerHandle
_wa_debounce_timers: dict[str, asyncio.TimerHandle] = {}

# chat_id → list of wa_context dicts (one per message in window)
_wa_debounce_buffer: dict[str, list[dict]] = {}

_WA_DEBOUNCE_SECONDS = 15
_WA_HISTORY_COUNT = 15  # messages of recent chat history to fetch for context


def get_wa_polling_enabled() -> bool:
    return _wa_polling_enabled


def set_wa_polling_enabled(enabled: bool) -> None:
    global _wa_polling_enabled
    _wa_polling_enabled = enabled
    logger.info("WA poll: polling %s", "enabled" if enabled else "paused")


# ---------------------------------------------------------------------------
# Trigger-fire helper
# ---------------------------------------------------------------------------

async def _fire_automation(automation_id: int, trigger_context: dict | None = None) -> None:
    """Core: load the automation, create a thread + run, invoke the supervisor.

    trigger_context: optional extra info injected into the prompt, e.g.
        {"file_path": "/path/to/new_file.txt"} for fs_new_in_folder triggers.
    """
    async with AsyncSessionLocal() as db:
        automation = await db.get(Automation, automation_id)
        if automation is None or not automation.enabled:
            return

        # Create a new thread for this run
        thread = Thread(
            title=f"[Auto] {automation.name[:60]}",
            model=automation.model,
        )
        db.add(thread)
        await db.flush()

        conversation_id: int | None = None

        # For non-email/non-fs triggers (e.g. cron), pre-create a conversation
        # so that telegram_ask has a conversation_id to attach to, enabling the
        # webhook to resume correctly when the user replies.
        if not trigger_context:
            from app.automations.conversations import create_conversation
            conversation_id = await create_conversation(
                automation_id=automation_id,
                trigger_kind="cron",
            )
            # Use a different prefix that allows telegram_ask (asking the user via Telegram
            # is how these automations communicate — it is NOT a clarifying question).
            _EXEC_PREFIX = (
                "[AUTOMATION RUN — execute immediately. "
                "Call tools directly as instructed. "
                "You MAY call telegram_ask to interact with the user via Telegram.]\n\n"
            )
            # Inject conversation_id prominently so the LLM passes it to telegram_ask
            effective_prompt = (
                _EXEC_PREFIX
                + automation.action_prompt
                + f"\n\n━━━ SYSTEM: conversation_id={conversation_id} ━━━"
                + f"\nYou MUST pass conversation_id={conversation_id} to EVERY telegram_ask call."
            )
        else:
            # For email/fs triggers, keep the original no-questions prefix
            _EXEC_PREFIX = (
                "[AUTOMATION RUN — execute immediately, no questions, no clarifications. "
                "Call tools directly as instructed. Do not ask the user anything "
                "(use telegram_ask if the action_prompt explicitly requires it).]\n\n"
            )
            effective_prompt = _EXEC_PREFIX + automation.action_prompt

        if trigger_context:
            if "email_from" in trigger_context:
                from app.automations.conversations import create_conversation
                conversation_id = await create_conversation(
                    automation_id=automation_id,
                    trigger_kind="gmail",
                    email_from=trigger_context.get("email_from", ""),
                    email_subject=trigger_context.get("email_subject", ""),
                    email_body=trigger_context.get("email_body", ""),
                    email_date=trigger_context.get("email_date", ""),
                )
                email_block = (
                    f"\n\n━━━ TRUSTED TRIGGER CONTEXT ━━━"
                    f"\nconversation_id: {conversation_id}"
                    f"\nrecipient_email: {trigger_context.get('email_from', '')}"
                    f"\nemail_subject: {trigger_context.get('email_subject', '(no subject)')}"
                    f"\nemail_date: {trigger_context.get('email_date', '')}"
                    f"\n\nemail_body:\n{trigger_context.get('email_body', '(no body)')}"
                    f"\n━━━ END TRUSTED CONTEXT ━━━"
                )
                effective_prompt = (
                    "[AUTOMATION RUN — execute immediately, no questions. "
                    "You MUST call tools as instructed. Do NOT just reply with text — "
                    "if the action says call telegram_send, you MUST call it as a tool. "
                    f"Always pass conversation_id={conversation_id} to telegram_ask. "
                    f"Use recipient_email from TRUSTED TRIGGER CONTEXT exactly as the 'to' arg for gmail_send.]\n\n"
                    + automation.action_prompt
                    + email_block
                )
            elif "file_path" in trigger_context:
                from app.automations.conversations import create_conversation
                conversation_id = await create_conversation(
                    automation_id=automation_id,
                    trigger_kind="fs",
                    file_path=trigger_context["file_path"],
                )
                effective_prompt = (
                    "[AUTOMATION RUN — execute immediately, no questions. "
                    "You MUST call tools as instructed. Do NOT just reply with text. "
                    "CRITICAL: You MUST call telegram_send as a real tool call — do NOT describe "
                    "what you would send, do NOT say 'I sent' without actually calling the tool. "
                    "If the action_prompt says to only notify for certain file types, you MUST still "
                    "call telegram_send for every file — either with the relevant content OR with a "
                    "message explaining the file was skipped (e.g. 'New file: X (not a PDF, skipped)'). "
                    "Never hallucinate a tool call — if you did not call telegram_send, do not claim you did.]\n\n"
                    + automation.action_prompt
                    + f"\n\nconversation_id: {conversation_id}"
                    + f"\nTriggered by new file: {trigger_context['file_path']}"
                )
            elif trigger_context.get("whatsapp"):
                trusted = trigger_context.get("trusted_block", "")
                effective_prompt = (
                    "[AUTOMATION RUN — execute immediately, no questions. "
                    "You MUST call tools as instructed. Do NOT just reply with text — "
                    "if the action says call telegram_send or whatsapp_send, you MUST call it as a tool. "
                    f"This run's thread_id is {thread.id} — include it ONLY in Telegram notifications as 'Thread: #{thread.id}'. "
                    "NEVER mention thread_id, Thread #, or any internal system info in WhatsApp group replies.]\n\n"
                    + automation.action_prompt
                    + trusted
                )

        # Save the action prompt as the first user message
        user_msg = Message(
            thread_id=thread.id,
            role="user",
            content=effective_prompt,
            metadata_json=json.dumps({"automation_id": automation_id, "automation_run": True}),
        )
        db.add(user_msg)

        # Create an AutomationRun record
        run = AutomationRun(
            automation_id=automation_id,
            started_at=datetime.now(timezone.utc),
            status="running",
            thread_id=thread.id,
        )
        db.add(run)

        # Update last_run_at on the automation
        automation.last_run_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(thread)
        await db.refresh(run)

        thread_id = thread.id
        run_id = run.id
        model = thread.model
        # Capture effective_prompt outside the db session for use below
        _effective_prompt = effective_prompt

    logger.info(
        "Automation %d (%s) fired → thread %d, run %d",
        automation_id,
        automation.name,
        thread_id,
        run_id,
    )

    # Use the conversation's LangGraph thread ID if one exists (multi-round reuse),
    # otherwise create a new one and save it back so continuations can reuse it.
    lg_thread_id = f"auto_{automation_id}_{run_id}"
    if conversation_id is not None:
        from app.automations.conversations import set_lg_thread
        # Save this lg_thread_id so the webhook can reuse it for continuations
        await set_lg_thread(conversation_id, lg_thread_id, thread_id)

    try:
        from langchain_core.messages import AIMessage, HumanMessage
        from app.agents.supervisor import get_graph

        graph = get_graph()
        lg_config = {
            "recursion_limit": 100,
            "configurable": {
                "thread_id": lg_thread_id,   # unique checkpoint key (string is fine for LangGraph)
                "ws_thread_id": thread_id,   # int for WebSocket routing in supervisor_node
                "model": model,
                "automation_run": True,      # skip interrupt() — no UI to approve
            },
        }

        lc_messages = [HumanMessage(content=_effective_prompt)]
        full_content: list[str] = []
        last_ai_content: str = ""

        async for event in graph.astream_events(
            {"messages": lc_messages}, lg_config, version="v2"
        ):
            event_type = event.get("event", "")
            # Collect streamed tokens
            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and chunk.content:
                    full_content.append(chunk.content)
            # Also capture final AI message from on_chain_end of supervisor node
            elif event_type == "on_chain_end" and event.get("name") == "supervisor":
                output = event.get("data", {}).get("output", {})
                msgs = output.get("messages", []) if isinstance(output, dict) else []
                for m in reversed(msgs):
                    if isinstance(m, AIMessage) and m.content:
                        last_ai_content = m.content if isinstance(m.content, str) else str(m.content)
                        break

        # Prefer streamed tokens; fall back to captured AI message
        final_content = "".join(full_content) or last_ai_content

        # Persist assistant reply
        if final_content:
            async with AsyncSessionLocal() as db2:
                msg = Message(
                    thread_id=thread_id,
                    role="assistant",
                    content=final_content,
                    metadata_json=json.dumps({"automation_id": automation_id}),
                )
                db2.add(msg)
                await db2.commit()
        else:
            logger.warning(
                "Automation %d run %d: no assistant content produced", automation_id, run_id
            )

        status = "done"

    except Exception as exc:
        logger.exception(
            "Automation %d run %d failed during supervisor execution: %s",
            automation_id,
            run_id,
            exc,
        )
        status = "failed"

    # Update run status
    async with AsyncSessionLocal() as db:
        run_obj = await db.get(AutomationRun, run_id)
        if run_obj is not None:
            run_obj.finished_at = datetime.now(timezone.utc)
            run_obj.status = status
            await db.commit()


async def _fire_automation_job(automation_id: int) -> None:
    """Thin async wrapper used as the APScheduler job coroutine."""
    await _fire_automation(automation_id)


# ---------------------------------------------------------------------------
# Gmail poll
# ---------------------------------------------------------------------------

async def _load_last_seen(automation_id: int) -> str | None:
    """Read last_seen_message_id from DB. Single source of truth — no in-memory cache."""
    async with AsyncSessionLocal() as db:
        automation = await db.get(Automation, automation_id)
        if automation:
            cfg = json.loads(automation.trigger_config_json)
            return cfg.get("last_seen_message_id")
    return None

async def _gmail_poll(automation_id: int, sender: str) -> None:
    """Poll Gmail for new messages from *sender* since last seen message."""
    # Ensure only one poll runs at a time per automation (prevents double-fire
    # when APScheduler fires a new job before the previous one finishes)
    if automation_id not in _gmail_poll_locks:
        _gmail_poll_locks[automation_id] = asyncio.Lock()
    lock = _gmail_poll_locks[automation_id]
    if lock.locked():
        logger.debug("Gmail poll automation %d: previous poll still running, skipping", automation_id)
        return
    async with lock:
        await _gmail_poll_inner(automation_id, sender)


async def _gmail_poll_inner(automation_id: int, sender: str) -> None:
    """Inner poll logic — always called under the per-automation lock."""
    try:
        from app.tools.google_tools import _get_gmail_service  # type: ignore
    except ImportError:
        logger.warning("Google tools not available; skipping Gmail poll for automation %d", automation_id)
        return

    try:
        service = await asyncio.to_thread(_get_gmail_service)

        last_id = await _load_last_seen(automation_id)

        if last_id is None:
            # First poll — only fetch messages that arrived after the automation was created.
            async with AsyncSessionLocal() as db:
                automation_obj = await db.get(Automation, automation_id)
                created_ts = int(automation_obj.created_at.replace(tzinfo=timezone.utc).timestamp()) if automation_obj else 0

            query = f"after:{created_ts}" if not sender else f"from:{sender} after:{created_ts}"
            result = await asyncio.to_thread(
                lambda: service.users().messages().list(userId="me", q=query, maxResults=10).execute()
            )
            messages = result.get("messages", [])

            if not messages:
                logger.info("Gmail poll automation %d: first poll, no mail yet — seeding sentinel", automation_id)
                await _persist_last_seen(automation_id, "0")
                return

            newest_id = messages[0]["id"]
            await _persist_last_seen(automation_id, newest_id)

            new_messages = [m["id"] for m in messages]
            logger.info(
                "Gmail poll automation %d: first poll, found %d new message(s) after creation",
                automation_id, len(new_messages)
            )
        else:
            # Normal poll — fetch all messages from sender (or any if sender is empty)
            query = "in:inbox" if not sender else f"from:{sender}"
            result = await asyncio.to_thread(
                lambda: service.users().messages().list(userId="me", q=query, maxResults=10).execute()
            )
            messages = result.get("messages", [])
            if not messages:
                logger.info("Gmail poll automation %d: no messages from %s", automation_id, sender)
                return

            newest_id = messages[0]["id"]

            if newest_id == last_id:
                logger.debug("Gmail poll automation %d: no new mail", automation_id)
                return  # nothing new

            # Update last_seen before firing to avoid double-firing on next poll
            await _persist_last_seen(automation_id, newest_id)

            # Find all new message IDs (everything before last_id in the list).
            # "0" is the sentinel for "no prior mail" — fire only the single newest.
            if last_id == "0":
                new_messages = [messages[0]["id"]]
            else:
                new_messages = []
                for m in messages:
                    if m["id"] == last_id:
                        break
                    new_messages.append(m["id"])

        if not new_messages:
            return

        # Fire once per new message (most recent first, cap at 3 to avoid flood)
        # Fetch all email contexts first, then fire concurrently
        async def _handle_one(msg_id: str) -> None:
            logger.info("Gmail poll automation %d: new mail from %s (id=%s)", automation_id, sender, msg_id)
            email_context = await _fetch_email_context(service, msg_id)
            await _fire_automation(
                automation_id,
                trigger_context={"gmail_message_id": msg_id, **email_context},
            )

        await asyncio.gather(*[_handle_one(mid) for mid in new_messages[:3]])

    except Exception as exc:
        logger.warning("Gmail poll automation %d error: %s", automation_id, exc)


async def _gmail_keyword_poll(automation_id: int, keywords: str) -> None:
    """Poll Gmail for new messages matching *keywords* since last seen message."""
    if automation_id not in _gmail_poll_locks:
        _gmail_poll_locks[automation_id] = asyncio.Lock()
    lock = _gmail_poll_locks[automation_id]
    if lock.locked():
        logger.debug("Gmail keyword poll automation %d: previous poll still running, skipping", automation_id)
        return
    async with lock:
        await _gmail_keyword_poll_inner(automation_id, keywords)


async def _gmail_keyword_poll_inner(automation_id: int, keywords: str) -> None:
    """Inner logic for keyword-based Gmail polling."""
    try:
        from app.tools.google_tools import _get_gmail_service  # type: ignore
    except ImportError:
        logger.warning("Google tools not available; skipping Gmail keyword poll for automation %d", automation_id)
        return

    try:
        service = await asyncio.to_thread(_get_gmail_service)

        last_id = await _load_last_seen(automation_id)

        if last_id is None:
            # First poll — only fetch messages that arrived after automation creation
            async with AsyncSessionLocal() as db:
                automation_obj = await db.get(Automation, automation_id)
                created_ts = int(automation_obj.created_at.replace(tzinfo=timezone.utc).timestamp()) if automation_obj else 0

            query = f"({keywords}) after:{created_ts}"
            result = await asyncio.to_thread(
                lambda: service.users().messages().list(userId="me", q=query, maxResults=10).execute()
            )
            messages = result.get("messages", [])

            if not messages:
                logger.info("Gmail keyword poll automation %d: first poll, no matches yet — seeding sentinel", automation_id)
                await _persist_last_seen(automation_id, "0")
                return

            newest_id = messages[0]["id"]
            await _persist_last_seen(automation_id, newest_id)

            new_messages = [m["id"] for m in messages]
            logger.info(
                "Gmail keyword poll automation %d: first poll, %d match(es) after creation",
                automation_id, len(new_messages)
            )
        else:
            query = keywords
            result = await asyncio.to_thread(
                lambda: service.users().messages().list(userId="me", q=query, maxResults=10).execute()
            )
            messages = result.get("messages", [])
            if not messages:
                return

            newest_id = messages[0]["id"]
            if newest_id == last_id:
                return

            await _persist_last_seen(automation_id, newest_id)

            if last_id == "0":
                new_messages = [messages[0]["id"]]
            else:
                new_messages = []
                for m in messages:
                    if m["id"] == last_id:
                        break
                    new_messages.append(m["id"])

        if not new_messages:
            return

        async def _handle_one(msg_id: str) -> None:
            logger.info("Gmail keyword poll automation %d: matched mail (id=%s)", automation_id, msg_id)
            email_context = await _fetch_email_context(service, msg_id)
            await _fire_automation(
                automation_id,
                trigger_context={"gmail_message_id": msg_id, **email_context},
            )

        await asyncio.gather(*[_handle_one(mid) for mid in new_messages[:3]])

    except Exception as exc:
        logger.warning("Gmail keyword poll automation %d error: %s", automation_id, exc)


async def _fetch_email_context(service: Any, message_id: str) -> dict:
    """Fetch subject, sender, date, and body snippet for a Gmail message."""
    try:
        msg = await asyncio.to_thread(
            lambda: service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
        )
        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from app.tools.google_tools import _decode_body
        body = _decode_body(msg.get("payload", {}))
        # Truncate long bodies
        if len(body) > 2000:
            body = body[:2000] + "\n... [truncated]"
        return {
            "email_from": headers.get("From", ""),
            "email_subject": headers.get("Subject", "") or "(no subject)",
            "email_date": headers.get("Date", ""),
            "email_body": body.strip(),
        }
    except Exception as exc:
        logger.warning("Could not fetch email context for %s: %s", message_id, exc)
        return {}


async def _persist_last_seen(automation_id: int, message_id: str) -> None:
    """Save last_seen_message_id into trigger_config_json so it survives restarts."""
    try:
        async with AsyncSessionLocal() as db:
            automation = await db.get(Automation, automation_id)
            if automation:
                cfg = json.loads(automation.trigger_config_json)
                cfg["last_seen_message_id"] = message_id
                automation.trigger_config_json = json.dumps(cfg)
                await db.commit()
    except Exception as exc:
        logger.warning("Failed to persist last_seen for automation %d: %s", automation_id, exc)


# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------

class _NewFileHandler(FileSystemEventHandler):
    """Fire an automation when a new file is created in the watched folder."""

    # Debounce window in seconds — Windows emits multiple on_created events
    # for the same file (create + write flushes). Ignore duplicates within this window.
    _DEBOUNCE_SECONDS = 5.0

    def __init__(
        self,
        automation_id: int,
        loop: asyncio.AbstractEventLoop,
        file_extensions: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._automation_id = automation_id
        self._loop = loop  # uvicorn's event loop, captured at registration time
        # Lowercase extensions without dot, e.g. ["pdf", "txt"]. Empty = all files.
        self._file_extensions: list[str] = [e.lower().lstrip(".") for e in (file_extensions or [])]
        # path → monotonic timestamp of last fire, for debouncing
        self._last_fired: dict[str, float] = {}

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        import time
        file_path = event.src_path

        # Debounce: skip if this exact path fired within the cooldown window
        now = time.monotonic()
        last = self._last_fired.get(file_path, 0.0)
        if now - last < self._DEBOUNCE_SECONDS:
            logger.debug(
                "fs_new_in_folder automation %d: debouncing duplicate event for %s",
                self._automation_id, file_path,
            )
            return
        self._last_fired[file_path] = now
        # Evict old entries to prevent unbounded growth
        cutoff = now - 60.0
        self._last_fired = {p: t for p, t in self._last_fired.items() if t > cutoff}

        # Extension filter: if configured, skip files that don't match
        if self._file_extensions:
            ext = Path(file_path).suffix.lower().lstrip(".")
            if ext not in self._file_extensions:
                logger.debug(
                    "fs_new_in_folder automation %d: skipping %s (ext %r not in %s)",
                    self._automation_id, file_path, ext, self._file_extensions,
                )
                return
        logger.info(
            "fs_new_in_folder automation %d: new file %s",
            self._automation_id, file_path,
        )
        # watchdog runs in a separate thread — schedule on uvicorn's loop
        asyncio.run_coroutine_threadsafe(
            _fire_automation(
                self._automation_id,
                trigger_context={"file_path": file_path},
            ),
            self._loop,
        )


# ---------------------------------------------------------------------------
# Register / unregister individual automations
# ---------------------------------------------------------------------------

def _register_automation(automation: Automation, loop: asyncio.AbstractEventLoop) -> None:
    """Register one automation's trigger. Safe to call even if already registered."""
    global _scheduler, _observer

    aid = automation.id
    trigger_type = automation.trigger_type
    config: dict = json.loads(automation.trigger_config_json)

    if trigger_type == "cron":
        cron_expr: str = config.get("cron", "*/5 * * * *")
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.warning("Automation %d: invalid cron %r — skipping", aid, cron_expr)
            return
        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week
        )
        job_id = f"cron_{aid}"
        if _scheduler and not _scheduler.get_job(job_id):
            _scheduler.add_job(
                _fire_automation_job,
                trigger=trigger,
                id=job_id,
                replace_existing=True,
                args=[aid],
                max_instances=1,          # never overlap two runs of the same automation
                misfire_grace_time=120,   # allow up to 2 min late (supervisor can be slow)
                coalesce=True,            # if several misfired, run only once
            )
            logger.info("Registered cron automation %d: %s", aid, cron_expr)

    elif trigger_type in ("gmail_new_from_sender", "gmail_any_new"):
        # sender="" or trigger_type="gmail_any_new" means match any sender
        sender: str = config.get("sender", "")
        job_id = f"gmail_{aid}"
        if _scheduler and not _scheduler.get_job(job_id):
            _scheduler.add_job(
                _gmail_poll,
                trigger=IntervalTrigger(minutes=1),
                id=job_id,
                replace_existing=True,
                args=[aid, sender],
                max_instances=1,
                misfire_grace_time=60,
                coalesce=True,
            )
            logger.info("Registered gmail poll automation %d: sender=%r", aid, sender or "any")

    elif trigger_type == "gmail_keyword_match":
        keywords: str = config.get("keywords", "")
        if not keywords:
            logger.warning("Automation %d: no keywords in config — skipping", aid)
            return
        job_id = f"gmail_{aid}"
        if _scheduler and not _scheduler.get_job(job_id):
            _scheduler.add_job(
                _gmail_keyword_poll,
                trigger=IntervalTrigger(minutes=1),
                id=job_id,
                replace_existing=True,
                args=[aid, keywords],
                max_instances=1,
                misfire_grace_time=60,
                coalesce=True,
            )
            logger.info("Registered gmail keyword poll automation %d: keywords=%r", aid, keywords)

    elif trigger_type == "fs_new_in_folder":
        folder: str = config.get("folder", "")
        if not folder:
            logger.warning("Automation %d: no folder in config — skipping", aid)
            return
        watch_path = str(Path(folder).resolve())
        # Ensure the folder exists
        Path(watch_path).mkdir(parents=True, exist_ok=True)
        if _observer and aid not in _fs_handlers:
            file_extensions: list[str] = config.get("file_extensions") or []
            handler = _NewFileHandler(aid, loop, file_extensions=file_extensions)
            watch = _observer.schedule(handler, watch_path, recursive=False)
            _fs_handlers[aid] = (watch, watch_path)
            logger.info(
                "Registered fs_new_in_folder automation %d: %s (filter: %s)",
                aid, watch_path, file_extensions or "all files",
            )
    elif trigger_type in ("whatsapp_group_new", "whatsapp_keyword_match", "whatsapp_outgoing_new", "whatsapp_smart_reply"):
        # Webhook-driven — no scheduler job needed; on_whatsapp_message() / on_whatsapp_outgoing() handles dispatch
        logger.info(
            "Registered whatsapp automation %d (%s) — webhook-driven", aid, trigger_type
        )
    else:
        logger.warning("Automation %d: unknown trigger_type %r", aid, trigger_type)


def unregister_automation(automation_id: int) -> None:
    """Remove a trigger for an automation (called on disable or delete)."""
    global _scheduler, _observer

    # Remove cron or gmail job
    for prefix in ("cron_", "gmail_"):
        job_id = f"{prefix}{automation_id}"
        if _scheduler and _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
            logger.info("Removed scheduler job %s", job_id)

    # Remove watchdog observer
    if automation_id in _fs_handlers and _observer:
        watch, _ = _fs_handlers.pop(automation_id)
        try:
            _observer.unschedule(watch)
        except Exception:
            pass
        logger.info("Removed fs watcher for automation %d", automation_id)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WhatsApp message polling (fallback for unreliable webhooks)
# ---------------------------------------------------------------------------

# Per-group set of already-seen message_ids to prevent overlap.
# Populated on first poll from DB so restarts don't re-fire old messages.
_wa_seen_ids: dict[str, set[str]] = {}  # chat_id → set of message_id strings
_wa_seen_lock = asyncio.Lock()

WHATSAPP_POLL_INTERVAL = 15  # seconds between polls
WHATSAPP_POLL_FETCH_COUNT = 100  # messages to fetch per poll — covers bursts well above normal group activity


async def _wa_poll_group(chat_id: str, group_name: str, registered_at_unix: float = 0.0) -> None:
    """Fetch recent messages for one group via Green API and store any new ones.

    Uses the in-memory _wa_seen_ids set (seeded from DB on startup) to skip
    messages already stored, so there is never any overlap regardless of how
    many times this runs.

    registered_at_unix: Unix timestamp of when the group was added to RAION.
    Messages older than this are skipped entirely (option-2 cutoff).
    """
    from app.integrations.green_api import get_green_client, GreenAPIError
    from app.db.models import WhatsAppMessage as _WAMsg

    client = get_green_client()
    if client is None:
        return

    try:
        raw_messages = await client.get_chat_history(chat_id, count=WHATSAPP_POLL_FETCH_COUNT)
    except GreenAPIError as exc:
        logger.debug("WA poll: getChatHistory failed for %s: %s", group_name, exc)
        return
    except Exception as exc:
        logger.debug("WA poll: unexpected error for %s: %s", group_name, exc)
        return

    if not raw_messages:
        return

    async with _wa_seen_lock:
        seen = _wa_seen_ids.setdefault(chat_id, set())

        new_messages = []
        for m in raw_messages:
            msg_id: str = m.get("idMessage", "")
            if not msg_id or msg_id in seen:
                continue

            # Skip messages older than the effective cutoff:
            # max of group registration time and server start time.
            # This prevents replaying history both on first group add and on server restart.
            effective_cutoff = max(registered_at_unix, _server_start_unix)
            if effective_cutoff and m.get("timestamp", 0) < effective_cutoff:
                seen.add(msg_id)  # mark as seen so we never re-check
                continue

            direction = m.get("type", "incoming")  # "incoming" | "outgoing"
            sender_name: str = m.get("senderName", "") or ""
            sender_id: str = m.get("senderId", "") or ""

            # Parse text and media_url from typeMessage field
            type_msg = m.get("typeMessage", "")
            media_url_poll = ""
            if type_msg == "textMessage":
                text = m.get("textMessage", "") or ""
            elif type_msg == "extendedTextMessage":
                text = (m.get("extendedTextMessage", {}) or {}).get("text", "") or m.get("textMessage", "") or ""
            elif type_msg in ("imageMessage", "videoMessage", "documentMessage"):
                msg_sub = m.get(type_msg, {}) or {}
                text = msg_sub.get("caption", "") or f"[{type_msg.replace('Message', '')}]"
                media_url_poll = msg_sub.get("downloadUrl", "")
            elif type_msg == "audioMessage":
                text = "[audio]"
            elif type_msg == "stickerMessage":
                text = "[sticker]"
            else:
                text = f"[{type_msg}]" if type_msg else ""

            # Map Green API typeMessage to our message_type string
            _type_map = {
                "imageMessage": "image",
                "videoMessage": "video",
                "documentMessage": "document",
                "audioMessage": "audio",
                "voiceMessage": "audio",
                "locationMessage": "location",
                "liveLocationMessage": "location",
            }
            mapped_type = _type_map.get(type_msg, "text")

            new_messages.append({
                "msg_id": msg_id,
                "direction": direction,
                "sender_id": sender_id,
                "sender_name": sender_name or None,
                "text": text or None,
                "type_msg": mapped_type,
                "media_url": media_url_poll,
            })

        if not new_messages:
            return

        # Persist new messages and mark seen
        async with AsyncSessionLocal() as db:
            # Double-check against DB to guard against seen-set loss on restart
            from sqlalchemy import select as _select
            existing = await db.execute(
                _select(_WAMsg.message_id).where(
                    _WAMsg.message_id.in_([m["msg_id"] for m in new_messages])
                )
            )
            already_in_db = {row[0] for row in existing.fetchall()}

            truly_new = [m for m in new_messages if m["msg_id"] not in already_in_db]
            if not truly_new:
                for m in new_messages:
                    seen.add(m["msg_id"])
                return

            for m in truly_new:
                db.add(_WAMsg(
                    message_id=m["msg_id"],
                    chat_id=chat_id,
                    sender_id=m["sender_id"],
                    sender_name=m["sender_name"],
                    direction=m["direction"],
                    message_type=m["type_msg"],
                    text=m["text"],
                    media_url=m.get("media_url") or None,
                ))
                seen.add(m["msg_id"])

            await db.commit()

        # Fire automation triggers for truly new messages
        for m in truly_new:
            if m["direction"] == "incoming":
                logger.info(
                    "WA poll: new incoming in %s from %s: %r",
                    group_name, m["sender_name"] or m["sender_id"], (m["text"] or "")[:60],
                )
                asyncio.create_task(
                    on_whatsapp_message(
                        chat_id=chat_id,
                        sender_id=m["sender_id"] or "",
                        sender_name=m["sender_name"] or "",
                        message_text=m["text"] or "",
                        group_name=group_name,
                        message_type=m["type_msg"],
                        media_url=m.get("media_url") or "",
                        message_id=m["msg_id"],
                    )
                )
            elif m["direction"] == "outgoing" and m["sender_id"] not in ("agent", "RAION"):
                # Phone-owner outgoing messages (sender_id is empty/None from Green API).
                # Fire on_whatsapp_outgoing so whatsapp_outgoing_new automations can react
                # (e.g. "log my replies to logs.txt"). Safe: these automations should call
                # telegram_send or write_file, NOT whatsapp_send — that would loop.
                logger.info(
                    "WA poll: new outgoing (phone owner) in %s: %r",
                    group_name, (m["text"] or "")[:60],
                )
                asyncio.create_task(
                    on_whatsapp_outgoing(
                        chat_id=chat_id,
                        message_text=m["text"] or "",
                        group_name=group_name,
                        sender_id=m["sender_id"] or "",
                        sender_name=m["sender_name"] or "",
                    )
                )


async def _wa_seed_seen_from_db() -> None:
    """On startup, load all already-stored message_ids into _wa_seen_ids.

    This prevents re-firing automations for messages that were stored in a
    previous session (before the polling loop started).
    """
    from sqlalchemy import select as _select
    from app.db.models import WhatsAppMessage as _WAMsg

    async with AsyncSessionLocal() as db:
        result = await db.execute(_select(_WAMsg.chat_id, _WAMsg.message_id))
        rows = result.fetchall()

    async with _wa_seen_lock:
        for chat_id, msg_id in rows:
            _wa_seen_ids.setdefault(chat_id, set()).add(msg_id)

    logger.info("WA poll: seeded seen-set with %d existing message IDs", len(rows))


async def _wa_poll_loop() -> None:
    """Background loop: polls all enabled WhatsApp groups every WHATSAPP_POLL_INTERVAL seconds."""
    from sqlalchemy import select as _select
    from app.db.models import WhatsAppGroup

    await _wa_seed_seen_from_db()
    logger.info("WA poll: background polling started (interval=%ds)", WHATSAPP_POLL_INTERVAL)

    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    _select(WhatsAppGroup).where(WhatsAppGroup.enabled == True)  # noqa: E712
                )
                groups = result.scalars().all()

            if groups and app_config.whatsapp_enabled() and _wa_polling_enabled:
                # Poll each group concurrently, passing registration timestamp as cutoff
                await asyncio.gather(
                    *[
                        _wa_poll_group(
                            g.chat_id,
                            g.name,
                            registered_at_unix=g.created_at.replace(tzinfo=timezone.utc).timestamp(),
                        )
                        for g in groups
                    ],
                    return_exceptions=True,
                )
        except Exception as exc:
            logger.warning("WA poll: loop error: %s", exc)

        await asyncio.sleep(WHATSAPP_POLL_INTERVAL)


async def start_automations_runtime() -> None:
    """Called from app startup. Initialises scheduler + observer, loads all enabled automations."""
    global _scheduler, _observer, _wa_poll_task, _server_start_unix, _runtime_started
    if _runtime_started:
        logger.warning("start_automations_runtime called more than once — ignoring duplicate")
        return
    _runtime_started = True
    _server_start_unix = datetime.now(timezone.utc).timestamp()

    loop = asyncio.get_running_loop()

    _scheduler = AsyncIOScheduler()
    _scheduler.start()

    _observer = Observer()
    _observer.start()

    # Load all enabled automations
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        result = await db.execute(
            select(Automation).where(Automation.enabled == True)  # noqa: E712
        )
        automations = result.scalars().all()

    for automation in automations:
        _register_automation(automation, loop)

    # Start WhatsApp polling loop if WhatsApp is configured
    if app_config.whatsapp_enabled():
        if _wa_poll_task is None or _wa_poll_task.done():
            _wa_poll_task = asyncio.create_task(_wa_poll_loop())
            logger.info("WA poll: task created")
        else:
            logger.info("WA poll: task already running, skipping duplicate start")

    logger.info(
        "Automations runtime started: %d automation(s) loaded", len(automations)
    )


async def stop_automations_runtime() -> None:
    """Called from app shutdown."""
    global _scheduler, _observer, _wa_poll_task, _runtime_started
    _runtime_started = False

    if _wa_poll_task and not _wa_poll_task.done():
        _wa_poll_task.cancel()
        try:
            await _wa_poll_task
        except asyncio.CancelledError:
            pass
        _wa_poll_task = None

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None

    if _observer:
        _observer.stop()
        _observer.join(timeout=5)
        _observer = None

    _fs_handlers.clear()
    logger.info("Automations runtime stopped")


# ---------------------------------------------------------------------------
# WhatsApp automation dispatch (webhook-driven)
# ---------------------------------------------------------------------------

async def _describe_whatsapp_image(chat_id: str, message_id: str) -> str:
    """Download a WhatsApp image via Green API and describe it with GPT-4o vision.

    Returns a plain-text description, or empty string on any error.
    """
    if not chat_id or not message_id:
        return ""
    try:
        import base64 as _b64
        from openai import AsyncOpenAI
        from app.integrations.green_api import get_green_client

        client_wa = get_green_client()
        if client_wa is None:
            logger.warning("_describe_whatsapp_image: WhatsApp client not configured")
            return ""

        image_bytes, content_type = await client_wa.download_file(chat_id, message_id)
        if not image_bytes:
            logger.warning("_describe_whatsapp_image: empty response for message_id=%s", message_id)
            return ""

        b64_data = _b64.b64encode(image_bytes).decode()
        data_url = f"data:{content_type};base64,{b64_data}"

        client_ai = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)
        response = await client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are analyzing an image sent on WhatsApp in a school admission campaigning context. "
                                "Describe what you see clearly and concisely. "
                                "If it is an admission form, form template, proof of campaigning work, or any school-related document, say so explicitly. "
                                "Include any visible text, names, numbers, or important details."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        },
                    ],
                }
            ],
            max_tokens=400,
        )
        description = response.choices[0].message.content or ""
        logger.info("WA image described (%d chars)", len(description))
        return description.strip()
    except Exception as exc:
        logger.warning("_describe_whatsapp_image failed: %s", exc)
        return ""


async def _fire_whatsapp_automation(automation_id: int, wa_context: dict) -> None:
    """Fire a WhatsApp-triggered automation with the incoming message context."""
    history = wa_context.get("recent_history", "")
    history_block = f"\n\nRecent chat history (last {_WA_HISTORY_COUNT} messages, oldest first):\n{history}" if history else ""

    # If the message is an image, describe it with GPT-4o vision first
    image_block = ""
    msg_type = wa_context.get("message_type", "text")
    chat_id_ctx = wa_context.get("chat_id", "")
    message_id_ctx = wa_context.get("message_id", "")
    logger.info("WA image check: message_type=%r chat_id=%r message_id=%r", msg_type, chat_id_ctx, message_id_ctx)
    if msg_type == "image" and chat_id_ctx and message_id_ctx:
        description = await _describe_whatsapp_image(chat_id_ctx, message_id_ctx)
        logger.info("WA image description result: %r", description[:100] if description else "(empty)")
        if description:
            image_block = f"\n\nImage description (analyzed by vision model):\n{description}"

    trusted_block = (
        f"\n\n━━━ TRUSTED TRIGGER CONTEXT ━━━"
        f"\nchat_id: {wa_context.get('chat_id', '')}"
        f"\nsender_id: {wa_context.get('sender_id', '')}"
        f"\nsender_name: {wa_context.get('sender_name', '')}"
        f"\ngroup_name: {wa_context.get('group_name', '')}"
        f"\nmessage_type: {wa_context.get('message_type', 'text')}"
        f"\nmessage_text: {wa_context.get('message_text', '')}"
        f"{image_block}"
        f"{history_block}"
        f"\n━━━ END TRUSTED CONTEXT ━━━"
    )
    await _fire_automation(
        automation_id,
        trigger_context={"whatsapp": True, "trusted_block": trusted_block, **wa_context},
    )


async def _triage_whatsapp_message(
    message_text: str,
    topic_description: str,
    reply_context: str,
) -> tuple[bool, str]:
    """Call gpt-4o-mini to decide if a WhatsApp message matches the topic and what to reply.

    Returns (should_reply, reply_text). reply_text is empty string if should_reply is False.
    """
    import json as _json
    from openai import AsyncOpenAI
    import app.config as _cfg

    client = AsyncOpenAI(api_key=_cfg.OPENAI_API_KEY)
    system = (
        "You are a WhatsApp message triage assistant. "
        "Given a message and a topic description, decide if the message is related to that topic. "
        "If yes, generate an appropriate reply using the provided reply context. "
        "Output ONLY a JSON object: {\"should_reply\": true/false, \"reply\": \"<reply text or empty string>\"}"
    )
    user = (
        f"TOPIC DESCRIPTION:\n{topic_description}\n\n"
        f"REPLY CONTEXT (what to say if relevant):\n{reply_context}\n\n"
        f"INCOMING MESSAGE:\n{message_text}"
    )
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        result = _json.loads(resp.choices[0].message.content or "{}")
        should_reply = bool(result.get("should_reply", False))
        reply_text = result.get("reply", "") if should_reply else ""
        return should_reply, reply_text
    except Exception as exc:
        logger.warning("_triage_whatsapp_message failed: %s", exc)
        return False, ""


async def _fire_whatsapp_smart_reply(automation_id: int, wa_context: dict) -> None:
    """Triage an incoming message and optionally send a direct reply or fire the supervisor."""
    async with AsyncSessionLocal() as db:
        automation = await db.get(Automation, automation_id)
        if automation is None or not automation.enabled:
            return
        config: dict = json.loads(automation.trigger_config_json)

    topic_description = config.get("topic_description", "")
    reply_context = config.get("reply_context", "")
    message_text = wa_context.get("message_text", "")
    chat_id = wa_context.get("chat_id", "")

    should_reply, reply_text = await _triage_whatsapp_message(
        message_text=message_text,
        topic_description=topic_description,
        reply_context=reply_context,
    )

    if not should_reply:
        logger.debug("smart_reply automation %d: no match for message=%r", automation_id, message_text[:80])
        return

    logger.info("smart_reply automation %d: matched, reply=%r", automation_id, reply_text[:80])

    if reply_text:
        # Fast path: send reply directly without spinning up the full supervisor
        from app.integrations.green_api import get_green_client
        import uuid as _uuid
        from app.db.models import WhatsAppMessage as _WAMsg
        client = get_green_client()
        if client:
            try:
                await client.send_message(chat_id, reply_text)
                async with AsyncSessionLocal() as _db:
                    _db.add(_WAMsg(
                        message_id=f"out_{_uuid.uuid4().hex}",
                        chat_id=chat_id,
                        sender_id="agent",
                        sender_name="RAION",
                        direction="outgoing",
                        message_type="text",
                        text=reply_text,
                    ))
                    await _db.commit()
            except Exception as exc:
                logger.warning("smart_reply direct send failed: %s", exc)
    else:
        # Slow path: fire full supervisor with context if reply_context needs tool use
        trusted_block = (
            f"\n\n━━━ TRUSTED TRIGGER CONTEXT ━━━"
            f"\nchat_id: {chat_id}"
            f"\nsender_name: {wa_context.get('sender_name', '')}"
            f"\nmessage_text: {message_text}"
            f"\n━━━ END TRUSTED CONTEXT ━━━"
        )
        await _fire_automation(
            automation_id,
            trigger_context={"whatsapp": True, "trusted_block": trusted_block, **wa_context},
        )


async def _fetch_recent_chat_history(chat_id: str) -> str:
    """Fetch last _WA_HISTORY_COUNT messages from Green API and format as readable block."""
    try:
        from app.integrations.green_api import get_green_client
        client = get_green_client()
        if client is None:
            return ""
        messages = await client.get_chat_history(chat_id, count=_WA_HISTORY_COUNT)
        if not messages:
            return ""
        lines = []
        for m in reversed(messages):  # oldest first
            sender = m.get("senderName") or m.get("senderId", "unknown")
            type_msg = m.get("typeMessage", "")
            if type_msg == "textMessage":
                text = m.get("textMessage", "")
            elif type_msg == "extendedTextMessage":
                text = (m.get("extendedTextMessage") or {}).get("text", "") or m.get("textMessage", "")
            elif type_msg in ("imageMessage", "videoMessage", "documentMessage"):
                text = (m.get(type_msg) or {}).get("caption", "") or f"[{type_msg.replace('Message', '')}]"
            elif type_msg == "audioMessage":
                text = "[audio]"
            elif type_msg:
                text = f"[{type_msg}]"
            else:
                text = ""
            if text:
                lines.append(f"  {sender}: {text}")
        return "\n".join(lines)
    except Exception as exc:
        logger.debug("_fetch_recent_chat_history failed: %s", exc)
        return ""


async def _flush_wa_debounce(chat_id: str) -> None:
    """Called after debounce window expires. Fires automations with all buffered messages."""
    buffered = _wa_debounce_buffer.pop(chat_id, [])
    _wa_debounce_timers.pop(chat_id, None)

    if not buffered:
        return

    # Use the last message's context as the base; combine all texts with sender attribution
    base = buffered[-1].copy()
    if len(buffered) > 1:
        combined_texts = "\n".join(
            f"[msg {i+1} from {m['sender_name'] or m['sender_id']}] {m['message_text']}"
            for i, m in enumerate(buffered)
        )
        base["message_text"] = combined_texts
        # Use the most severe message_type seen (image/video/audio/document > text)
        type_priority = {"image": 5, "video": 4, "audio": 3, "document": 2, "location": 1, "text": 0}
        base["message_type"] = max(
            (m["message_type"] for m in buffered),
            key=lambda t: type_priority.get(t, 0),
        )
        # Carry message_id and media_url from the first image message in the batch (if any)
        image_msg = next((m for m in buffered if m.get("message_type") == "image" and m.get("message_id")), None)
        if image_msg:
            base["media_url"] = image_msg.get("media_url", "")
            base["message_id"] = image_msg["message_id"]

    # Fetch recent chat history for LLM context
    history = await _fetch_recent_chat_history(chat_id)
    if history:
        base["recent_history"] = history

    await on_whatsapp_message_fire(base)


async def on_whatsapp_message(
    chat_id: str,
    sender_id: str,
    sender_name: str,
    message_text: str,
    group_name: str = "",
    message_type: str = "text",
    media_url: str = "",
    message_id: str = "",
) -> None:
    """Called by the WhatsApp webhook handler for every incoming message.

    Buffers the message and starts/resets a 15s debounce timer per sender.
    After 15s of silence the batch is flushed and automations are fired once
    with combined message text + recent chat history for context.
    """
    wa_context = {
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message_text": message_text,
        "group_name": group_name,
        "message_type": message_type,
        "media_url": media_url,
        "message_id": message_id,
    }

    _wa_debounce_buffer.setdefault(chat_id, []).append(wa_context)

    # Cancel existing timer if any
    existing = _wa_debounce_timers.pop(chat_id, None)
    if existing:
        existing.cancel()

    # Schedule flush after debounce window
    loop = asyncio.get_running_loop()
    handle = loop.call_later(
        _WA_DEBOUNCE_SECONDS,
        lambda: asyncio.create_task(_flush_wa_debounce(chat_id)),
    )
    _wa_debounce_timers[chat_id] = handle


async def on_whatsapp_message_fire(wa_context: dict) -> None:
    """Actually match and fire WhatsApp automations for a (possibly batched) message context."""
    from sqlalchemy import select

    chat_id = wa_context.get("chat_id", "")
    sender_id = wa_context.get("sender_id", "")
    message_text = wa_context.get("message_text", "")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Automation).where(
                Automation.enabled == True,  # noqa: E712
                Automation.trigger_type.in_(["whatsapp_group_new", "whatsapp_keyword_match", "whatsapp_smart_reply"]),
            )
        )
        automations = result.scalars().all()

    for automation in automations:
        config: dict = json.loads(automation.trigger_config_json)

        if automation.trigger_type == "whatsapp_group_new":
            target_chat = config.get("chat_id", "")
            if not target_chat or target_chat == chat_id:
                logger.info(
                    "whatsapp_group_new automation %d matched chat_id=%s", automation.id, chat_id
                )
                asyncio.create_task(_fire_whatsapp_automation(automation.id, wa_context))

        elif automation.trigger_type == "whatsapp_keyword_match":
            keywords_raw = config.get("keywords", "")
            if not keywords_raw:
                continue
            keywords = [k.strip().lower() for k in keywords_raw.replace(",", " ").split() if k.strip()]
            text_lower = message_text.lower()
            if any(kw in text_lower for kw in keywords):
                logger.info(
                    "whatsapp_keyword_match automation %d matched text=%r", automation.id, message_text[:80]
                )
                asyncio.create_task(_fire_whatsapp_automation(automation.id, wa_context))

        elif automation.trigger_type == "whatsapp_smart_reply":
            if sender_id in ("agent", app_config.GREEN_API_INSTANCE_ID):
                continue
            target_chat = config.get("chat_id", "")
            if not target_chat or target_chat == chat_id:
                logger.info(
                    "whatsapp_smart_reply automation %d: triaging message from chat_id=%s",
                    automation.id, chat_id,
                )
                asyncio.create_task(_fire_whatsapp_smart_reply(automation.id, wa_context))


async def on_whatsapp_outgoing(
    chat_id: str,
    message_text: str,
    group_name: str = "",
    sender_id: str = "agent",
    sender_name: str = "RAION",
) -> None:
    """Called when an outgoing WhatsApp message is stored (sent by agent, manual UI, or phone owner).

    Fires all enabled whatsapp_outgoing_new automations that match the chat_id.
    sender_id/sender_name default to 'agent'/'RAION' for RAION-sent messages;
    pass empty string for phone-owner messages captured via polling.
    """
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Automation).where(
                Automation.enabled == True,  # noqa: E712
                Automation.trigger_type == "whatsapp_outgoing_new",
            )
        )
        automations = result.scalars().all()

    wa_context = {
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": sender_name or "you (phone owner)",
        "message_text": message_text,
        "group_name": group_name,
    }

    for automation in automations:
        config: dict = json.loads(automation.trigger_config_json)
        target_chat = config.get("chat_id", "")
        if not target_chat or target_chat == chat_id:
            logger.info(
                "whatsapp_outgoing_new automation %d matched chat_id=%s", automation.id, chat_id
            )
            asyncio.create_task(_fire_whatsapp_automation(automation.id, wa_context))


# ---------------------------------------------------------------------------
# Public helpers used by the web routes
# ---------------------------------------------------------------------------

async def register_new_automation(automation: Automation) -> None:
    """Called after creating a new automation to register it immediately (if enabled)."""
    if not automation.enabled:
        return
    loop = asyncio.get_running_loop()
    _register_automation(automation, loop)


async def disable_automation(automation_id: int) -> None:
    """Called when an automation is disabled via the UI."""
    unregister_automation(automation_id)


async def enable_automation(automation: Automation) -> None:
    """Called when an automation is re-enabled via the UI."""
    loop = asyncio.get_running_loop()
    _register_automation(automation, loop)


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the running scheduler, or None if not started yet."""
    return _scheduler
