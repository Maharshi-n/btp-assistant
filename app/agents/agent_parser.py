"""Agent creation: interview questions + finalize role_block & triggers (gpt-5.4-mini)."""
from __future__ import annotations

import json

from openai import AsyncOpenAI

import app.config as app_config

VALID_TRIGGER_TYPES = {
    "cron", "gmail_any_new", "gmail_new_from_sender", "gmail_keyword_match",
    "fs_new_in_folder", "whatsapp_group_new", "whatsapp_keyword_match",
    "whatsapp_outgoing_new", "whatsapp_smart_reply",
}

_INTERVIEW_SYSTEM = (
    "You set up a long-lived AI agent for the user. Given a role description, ask ONLY "
    "the clarifying questions you genuinely need to make the agent a master of this role "
    "(e.g. thresholds, schedule, which data, where to notify). Skip anything inferable. "
    "Output ONLY a JSON object: {\"questions\": [\"...\", ...]}. Empty list if nothing is needed."
)

_FINALIZE_SYSTEM = (
    "You finalize an AI agent's setup. Given the role description and the user's interview "
    "answers, output ONLY a JSON object:\n"
    "{\"role_block\": \"<the 'You are ...' identity/role text baked with the answers>\", "
    "\"triggers\": [{\"trigger_type\": \"<one of the allowed types>\", \"trigger_config\": {...}}]}\n"
    f"Allowed trigger_type values: {sorted(VALID_TRIGGER_TYPES)}.\n"
    "cron config = {\"cron\": \"<5-field cron>\"}. An agent may have multiple triggers. "
    "Never invent a trigger_type outside the allowed set."
)


def validate_triggers(triggers: list[dict]) -> None:
    """Raise ValueError if any trigger is malformed."""
    if not isinstance(triggers, list) or not triggers:
        raise ValueError("triggers must be a non-empty list")
    for t in triggers:
        tt = t.get("trigger_type")
        if tt not in VALID_TRIGGER_TYPES:
            raise ValueError(f"Invalid trigger_type {tt!r}")
        cfg = t.get("trigger_config", {})
        if tt == "cron":
            cron = cfg.get("cron", "")
            if len(cron.split()) != 5:
                raise ValueError(f"Invalid cron expression {cron!r}")


async def _call_llm(messages: list[dict]) -> str:
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)
    resp = await client.chat.completions.create(
        model="gpt-5.4-mini",
        messages=messages,
        temperature=0,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or "{}"


async def interview_questions(role_description: str, context_block: str = "") -> list[str]:
    raw = await _call_llm([
        {"role": "system", "content": _INTERVIEW_SYSTEM},
        {"role": "user", "content": f"{context_block}\nROLE:\n{role_description}"},
    ])
    return list(json.loads(raw).get("questions", []))


async def finalize_agent_spec(role_description: str, interview_answers: str) -> dict:
    raw = await _call_llm([
        {"role": "system", "content": _FINALIZE_SYSTEM},
        {"role": "user", "content": f"ROLE:\n{role_description}\n\nANSWERS:\n{interview_answers}"},
    ])
    spec = json.loads(raw)
    if "role_block" not in spec or "triggers" not in spec:
        raise ValueError(f"Agent spec missing keys: {spec}")
    validate_triggers(spec["triggers"])
    return spec
