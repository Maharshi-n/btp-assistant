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
