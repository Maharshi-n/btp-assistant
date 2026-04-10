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
