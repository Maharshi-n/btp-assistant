from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY: str = os.environ.get("SECRET_KEY", "change-me-in-production")
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'app.db'}"
)

ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "maharshi")
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")

SESSION_TTL_SECONDS: int = int(os.environ.get("SESSION_TTL_SECONDS", str(7 * 24 * 3600)))

OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

WORKSPACE_DIR: Path = Path(
    os.environ.get("WORKSPACE_DIR", str(BASE_DIR / "workspace"))
).resolve()

GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")

FERNET_KEY: str = os.environ.get("FERNET_KEY", "")

# Telegram notifications
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

TELEGRAM_WEBHOOK_URL: str = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
TELEGRAM_WEBHOOK_SECRET: str = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

# WhatsApp (Green API)
GREEN_API_BASE_URL: str = os.getenv("GREEN_API_BASE_URL", "https://api.green-api.com")
GREEN_API_INSTANCE_ID: str = os.getenv("GREEN_API_INSTANCE_ID", "")
GREEN_API_TOKEN: str = os.getenv("GREEN_API_TOKEN", "")
GREEN_API_WEBHOOK_TOKEN: str = os.getenv("GREEN_API_WEBHOOK_TOKEN", "")


def whatsapp_enabled() -> bool:
    return bool(GREEN_API_INSTANCE_ID and GREEN_API_TOKEN)


DEFAULT_THREAD_MODEL: str = os.environ.get("DEFAULT_THREAD_MODEL", "gpt-4o-mini")
