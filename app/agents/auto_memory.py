"""Auto-memory extraction — runs after each assistant response.

Extracts memorable facts from the conversation and saves them to UserMemory.
Only runs when auto-memory is enabled in AutoMemoryConfig.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a memory extraction assistant. Given a conversation snippet, extract any facts \
about the user that are worth remembering long-term — preferences, habits, personal details, \
recurring needs, or stated goals.

Rules:
- Only extract facts explicitly stated by the user (not inferred).
- Skip facts already too generic or obvious.
- Skip task results (e.g. "the file was saved") — only extract user-specific facts.
- Output a JSON array of strings. Each string is one concise fact, max 20 words.
- If nothing is worth remembering, output an empty array: []
- Output ONLY the JSON array, no explanation.

Examples of good memories:
- "Prefers concise responses without preamble"
- "Works in IST timezone"
- "Uses Notion for task management"
- "Prefers PDF reports over text files"
- "Morning routine starts at 6am"

Examples of bad memories (skip these):
- "Asked about the weather" (task, not a fact)
- "The file was saved to workspace" (result)
- "User said hello" (not memorable)"""


async def extract_and_save_memories(user_message: str, assistant_response: str) -> int:
    """Extract memorable facts from a conversation turn and save to DB.

    Returns the number of new memories saved.
    """
    try:
        from app.db.engine import AsyncSessionLocal
        from app.db.models import AutoMemoryConfig, UserMemory
        from sqlalchemy import select

        # Check if auto-memory is enabled
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutoMemoryConfig).limit(1))
            cfg = result.scalars().first()
            if not cfg or not cfg.enabled:
                return 0

        # Call OpenAI to extract memories
        import app.config as app_config
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)
        conversation = f"User: {user_message[:1000]}\n\nAssistant: {assistant_response[:1000]}"

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": conversation},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=256,
        )

        import json
        raw = response.choices[0].message.content or "[]"
        # Handle both {"memories": [...]} and [...] formats
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            facts = parsed.get("memories", parsed.get("facts", []))
        else:
            facts = parsed

        if not isinstance(facts, list):
            return 0

        facts = [f.strip() for f in facts if isinstance(f, str) and f.strip()]
        if not facts:
            return 0

        # Save to DB, skip duplicates
        async with AsyncSessionLocal() as db:
            existing_result = await db.execute(select(UserMemory))
            existing = {m.content for m in existing_result.scalars().all()}

            saved = 0
            for fact in facts:
                if fact not in existing:
                    db.add(UserMemory(content=fact))
                    saved += 1

            if saved:
                await db.commit()
                from app.agents.supervisor import invalidate_memory_cache
                invalidate_memory_cache()
                logger.info("Auto-memory: saved %d new facts", saved)

            return saved

    except Exception as exc:
        logger.warning("Auto-memory extraction failed: %s", exc)
        return 0
