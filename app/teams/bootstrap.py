"""AI-powered agent bootstrap: parse persona → refined prompt + crons + memory + tools."""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

import app.config as app_config

logger = logging.getLogger(__name__)

AVAILABLE_TOOLS = [
    "whatsapp_send", "whatsapp_send_file", "whatsapp_read_messages",
    "whatsapp_fetch_messages", "whatsapp_get_groups",
    "telegram_send", "telegram_ask", "telegram_send_file",
    "gmail_list_unread", "gmail_read", "gmail_send",
    "web_search", "web_fetch",
    "read_file", "write_file", "list_files", "delete_file",
    "run_shell_command",
    "generate_image",
    "rag_search", "rag_ingest",
    "write_memory", "promote_memory",
    "message_agent", "invoke_agent", "convene_council",
    "create_agent_cron", "edit_agent_cron", "delete_agent_cron",
]

_SYSTEM = """You are an agent configuration expert. Given a user's natural-language description
of what they want an AI agent to do, you extract a structured configuration.

Return a JSON object with exactly these fields:

{
  "refined_persona": "A clean, focused system prompt for the agent. Should describe behavior
    precisely without over-instructing. Remove any cron scheduling instructions from the persona —
    those go in the crons array. Keep it under 800 words.",

  "crons": [
    {
      "name": "human-readable name",
      "cron_expr": "standard 5-part UTC cron expression",
      "action_prompt": "exact instruction the agent should follow when this cron fires"
    }
  ],

  "important_memory": [
    {
      "key": "snake_case_key",
      "content": "value to store"
    }
  ],

  "suggested_tools": ["tool_name_1", "tool_name_2"],

  "wake_mode": "always_on | event_only | scheduled_only | adaptive | manual",

  "tick_interval_seconds": 30,

  "reasoning": "One sentence explaining the key design decisions made."
}

Rules:
- All cron times must be in UTC. India Standard Time = UTC+5:30, so 9:00 AM IST = 03:30 UTC.
- Only include crons the user explicitly mentioned or that are clearly implied.
- Only include tools from this list: """ + json.dumps(AVAILABLE_TOOLS) + """
- wake_mode should be event_only if the agent primarily reacts to messages,
  scheduled_only if it only runs on a timer, always_on if it does both.
- important_memory should seed facts the agent needs on day 1 (roster info, group IDs, etc.)
- refined_persona should NOT contain cron schedules — those are separate.
- Return ONLY the JSON object, no markdown, no explanation."""


async def bootstrap_agent(
    persona: str,
    parse_model: str = "gpt-4o",
    existing_cron_names: list[str] | None = None,
) -> dict[str, Any]:
    """Call GPT to parse a persona description into structured agent config.

    Returns dict with keys: refined_persona, crons, important_memory,
    suggested_tools, wake_mode, tick_interval_seconds, reasoning.
    """
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)

    user_msg = f"Agent description:\n\n{persona}"
    if existing_cron_names:
        user_msg += f"\n\nNote: These crons already exist, do not recreate them: {existing_cron_names}"

    try:
        response = await client.chat.completions.create(
            model=parse_model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        result = json.loads(raw)
    except Exception as exc:
        logger.error("bootstrap_agent: GPT call failed: %s", exc)
        return {
            "error": str(exc),
            "refined_persona": persona,
            "crons": [],
            "important_memory": [],
            "suggested_tools": [],
            "wake_mode": "event_only",
            "tick_interval_seconds": 30,
            "reasoning": "Parse failed — original persona kept.",
        }

    # Validate and sanitize
    result.setdefault("refined_persona", persona)
    result.setdefault("crons", [])
    result.setdefault("important_memory", [])
    result.setdefault("suggested_tools", [])
    result.setdefault("wake_mode", "event_only")
    result.setdefault("tick_interval_seconds", 30)
    result.setdefault("reasoning", "")

    # Only allow known tools
    result["suggested_tools"] = [
        t for t in result["suggested_tools"] if t in AVAILABLE_TOOLS
    ]

    return result
