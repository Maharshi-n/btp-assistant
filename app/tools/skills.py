"""Skills tool — lets the supervisor read a named skill file on demand."""
from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.tools import tool

import app.config as app_config

logger = logging.getLogger(__name__)


@tool
async def read_skill(name: str) -> str:
    """Read the full content of a named skill file.

    Use this when a skill listed in the SKILLS section of your context is
    relevant to the user's request. Pass the exact skill name as shown in
    the list.

    Args:
        name: The exact name of the skill (e.g. "negotiation", "mcp_notion").

    Returns:
        The full text content of the skill file, or an error if not found.
    """
    from app.db.engine import AsyncSessionLocal
    from app.db.models import Skill

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        result = await db.execute(
            select(Skill).where(Skill.name == name).where(Skill.enabled == True)  # noqa: E712
        )
        skill = result.scalars().first()

    if skill is None:
        return f"Skill '{name}' not found or is disabled."

    raw = Path(skill.file_path)
    path = raw if raw.is_absolute() else app_config.WORKSPACE_DIR / raw
    if not path.exists():
        logger.warning("Skill '%s' file missing at %s", name, path)
        return f"Skill '{name}' file is missing from disk."

    try:
        content = path.read_text(encoding="utf-8")
        logger.info("read_skill: loaded '%s' (%d chars)", name, len(content))
        return content
    except Exception as exc:
        logger.warning("read_skill: failed to read '%s': %s", name, exc)
        return f"Failed to read skill '{name}': {exc}"
