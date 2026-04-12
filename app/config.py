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

# Phase 1 — single admin user seeded on first startup
ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "maharshi")
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")

# Session lifetime in seconds (7 days)
SESSION_TTL_SECONDS: int = int(os.environ.get("SESSION_TTL_SECONDS", str(7 * 24 * 3600)))

# Phase 3 — OpenAI API key for streaming chat completions
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

# Phase 5 — workspace directory the assistant is allowed to touch
WORKSPACE_DIR: Path = Path(
    os.environ.get("WORKSPACE_DIR", str(BASE_DIR / "workspace"))
).resolve()

# Phase 8 — Google OAuth credentials (Desktop app type)
GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# Fernet key for encrypting OAuth tokens at rest.
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY: str = os.environ.get("FERNET_KEY", "")

# Telegram notifications
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# Telegram webhook (bidirectional chat)
TELEGRAM_WEBHOOK_URL: str = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
TELEGRAM_WEBHOOK_SECRET: str = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
