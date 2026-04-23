from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


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
    provider: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    token_json: Mapped[str] = mapped_column(Text, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class PermissionAudit(Base):
    __tablename__ = "permission_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    args_json: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)


class Automation(Base):
    __tablename__ = "automations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    raw_description: Mapped[str] = mapped_column(Text, nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    action_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False, server_default="gpt-4o-mini")
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
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
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
    continuation_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    last_question: Mapped[str] = mapped_column(Text, nullable=True)
    thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(_DT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class TelegramPendingFile(Base):
    __tablename__ = "telegram_pending_files"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    intent_text: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=True)
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
    transport: Mapped[str] = mapped_column(String(8), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(String(512), nullable=True)
    env_encrypted: Mapped[str] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
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
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    input_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
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
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    context_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    lg_thread_id: Mapped[str] = mapped_column(String(128), nullable=True)
    db_thread_id: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


class TelegramCommand(Base):
    __tablename__ = "telegram_commands"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(256), nullable=False)
    preset_prompt: Mapped[str] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False, server_default="gpt-4o-mini")
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _DT, server_default=func.now(), nullable=False
    )


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


class WhatsAppGroup(Base):
    __tablename__ = "whatsapp_groups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    keyword_filter: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_send_allowed: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(_DT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_DT, server_default=func.now(), nullable=False)


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        Index("ix_wa_msg_chat_created", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sender_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_DT, server_default=func.now(), nullable=False)
