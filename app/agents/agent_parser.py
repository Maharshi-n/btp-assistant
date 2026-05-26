"""Agent creation: interview questions + finalize role_block & triggers (gpt-5.4-mini)."""
from __future__ import annotations

import json

from openai import AsyncOpenAI

import app.config as app_config

# Only trigger types that actually WAKE an agent today are allowed at creation
# time — otherwise a user could create an agent that silently never fires.
# fire_agent is wired for cron (scheduler) and the two incoming-WhatsApp types
# dispatched in app/automations/runtime.on_whatsapp_message_fire.
VALID_TRIGGER_TYPES = {
    "cron", "whatsapp_group_new", "whatsapp_keyword_match",
}

# Modelled + validated by the automation layer but NOT yet wired to fire_agent
# (gmail polling, whatsapp outgoing/smart-reply, fs). Kept here so re-enabling is
# a one-line move into VALID_TRIGGER_TYPES once the dispatch wiring lands.
DEFERRED_TRIGGER_TYPES = {
    "gmail_any_new", "gmail_new_from_sender", "gmail_keyword_match",
    "fs_new_in_folder", "whatsapp_outgoing_new", "whatsapp_smart_reply",
}

_INTERVIEW_SYSTEM = (
    "You set up a long-lived AI agent. Given a role description, ask the FEWEST possible "
    "clarifying questions — ONLY things you genuinely cannot infer and that would change what "
    "the agent does. Aim for 0-2 questions; never more than 3.\n"
    "INFER sensible defaults instead of asking — do NOT ask about any of these:\n"
    "- timing tolerance / exact-vs-approximate intervals (assume best-effort)\n"
    "- whether to run indefinitely or stop (assume indefinitely until the user disables it)\n"
    "- retry/error-handling behaviour (assume best-effort, skip on failure)\n"
    "- start time / timezone (assume start now; IST unless stated)\n"
    "- whether a message is always the same (assume yes unless the role implies otherwise)\n"
    "ONLY ask for genuinely missing specifics the agent cannot work without — e.g. WHICH "
    "Telegram chat / WhatsApp group / recipient, a threshold value, or which data source, when "
    "the role doesn't say. If the role is already actionable, return an empty list.\n"
    "Output ONLY a JSON object: {\"questions\": [\"...\", ...]}."
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
