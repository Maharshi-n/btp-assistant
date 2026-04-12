from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(256), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="New Chat")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="gpt-4o")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
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
        DateTime, server_default=func.now(), nullable=False
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
        DateTime, server_default=func.now(), nullable=False
    )
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    # Unique request id so the UI card can reference it
    request_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)


class Automation(Base):
    __tablename__ = "automations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # trigger_type: "cron" | "gmail_new_from_sender" | "fs_new_in_folder"
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # JSON blob: cron expr, sender address, or folder path depending on trigger_type
    trigger_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    action_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # status: "running" | "done" | "failed"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    # thread_id of the chat thread created for this run (nullable until created)
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)


class TelegramPendingReply(Base):
    __tablename__ = "telegram_pending_replies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Full prompt to feed the supervisor when the user replies
    continuation_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # DB thread_id of the automation run that created this pending reply
    thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Row expires after 24h — stale entries are ignored
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
