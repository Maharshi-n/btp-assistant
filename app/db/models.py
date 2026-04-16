from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base

# All DateTime columns use timezone=True so PostgreSQL stores TIMESTAMPTZ
# and accepts timezone-aware datetimes (e.g. datetime.now(timezone.utc)).
_DT = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(256), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(_DT, nullable=False)


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="New Chat")
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="gpt-4o-mini")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    metadata_json = Column(Text, nullable=True)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # e.g. "google"
    provider: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    # Fernet-encrypted JSON blob of the token dict from google-auth
    token_json: Mapped[str] = mapped_column(Text, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class PermissionAudit(Base):
    __tablename__ = "permission_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    args_json: Mapped[str] = mapped_column(Text, nullable=False)
    # decision: "auto" | "approved" | "denied"
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    # decided_by: "policy" | "user"
    decided_by: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    # Unique request id so the UI card can reference it
    request_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)


class Automation(Base):
    __tablename__ = "automations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # trigger_type: "cron" | "gmail_any_new" | "gmail_new_from_sender" | "gmail_keyword_match" | "fs_new_in_folder"
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # JSON blob: cron expr, sender address, or folder path depending on trigger_type
    trigger_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    action_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_run_at: Mapped[datetime] = mapped_column(_DT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(_DT, nullable=True)
    # status: "running" | "done" | "failed"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    # thread_id of the chat thread created for this run (nullable until created)
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)


class UserMemory(Base):
    __tablename__ = "user_memories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class TelegramPendingReply(Base):
    __tablename__ = "telegram_pending_replies"
    __table_args__ = (
        Index("ix_pending_chat_expires", "chat_id", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Full prompt to feed the supervisor when the user replies
    continuation_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # The exact question/draft that was shown to the user — injected into next continuation
    last_question: Mapped[str] = mapped_column(Text, nullable=True)
    # DB thread_id of the automation run that created this pending reply
    thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # FK to AutomationConversation — carries structured state so LLM doesn't re-derive it
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    # Row expires after 24h — stale entries are ignored
    expires_at: Mapped[datetime] = mapped_column(_DT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class TelegramPendingFile(Base):
    __tablename__ = "telegram_pending_files"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Telegram chat ID — one row per chat (upsert pattern)
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # The user's instruction text ("save this in reports/")
    intent_text: Mapped[str] = mapped_column(Text, nullable=False)
    # DB thread to post messages into (nullable — may not have an active thread)
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True)
    # AutomationConversation id if triggered from an automation (nullable)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=True)
    # Row expires after 10 minutes — stale entries are ignored
    expires_at: Mapped[datetime] = mapped_column(_DT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class TelegramPendingFileItem(Base):
    """One row per file accumulated for multi-file batch processing.

    Files accumulate here when the user sends multiple files before giving
    an intent. When the user sends a text (the intent), or types 'done',
    all items for that chat_id are fetched, processed together, then deleted.
    """
    __tablename__ = "telegram_pending_file_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_DT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    trigger_description: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # "stdio" | "sse"
    transport: Mapped[str] = mapped_column(String(8), nullable=False)
    # stdio: full command string e.g. "npx -y @notionhq/notion-mcp-server"
    command: Mapped[str] = mapped_column(Text, nullable=True)
    # sse: full URL e.g. "http://localhost:3000/mcp"
    url: Mapped[str] = mapped_column(String(512), nullable=True)
    # Fernet-encrypted JSON dict of env vars (tokens etc.)
    env_encrypted: Mapped[str] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    # "unknown" | "ok" | "error"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class MCPTool(Base):
    __tablename__ = "mcp_tools"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Prefixed tool name: mcp__<server_name>__<tool_name>
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    # JSON schema of input parameters
    input_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # "auto" | "ask"
    permission: Mapped[str] = mapped_column(String(8), nullable=False, default="ask")
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    cron: Mapped[str] = mapped_column(String(64), nullable=False)
    action_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_run_at: Mapped[datetime] = mapped_column(_DT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class ScheduledTaskRun(Base):
    __tablename__ = "scheduled_task_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_tasks.id"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(_DT, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)


class AutoMemoryConfig(Base):
    __tablename__ = "auto_memory_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)


class AutomationConversation(Base):
    __tablename__ = "automation_conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id"), nullable=True, index=True
    )
    # What kind of trigger started this conversation — informational only
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # "gmail" | "fs" | "cron" | "manual"
    # Frozen trigger context — arbitrary key/value pairs, set once, never modified
    context_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Evolving state — updated round by round (e.g. current draft text)
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # LangGraph checkpoint thread ID — reused across all continuation rounds
    # so the LLM sees the full conversation history every time.
    lg_thread_id: Mapped[str] = mapped_column(String(128), nullable=True)
    # DB thread_id of the first automation run that started this conversation
    db_thread_id: Mapped[int] = mapped_column(Integer, nullable=True)
    # Lifecycle: "active" | "done" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
