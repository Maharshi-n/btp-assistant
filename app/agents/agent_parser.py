"""Agent creation: interview questions + finalize role_block & triggers (gpt-5.4-mini)."""
from __future__ import annotations

import json

from openai import AsyncOpenAI

import app.config as app_config

# Trigger types an agent can be created with. All of these are wired to
# fire_agent: cron + WhatsApp via app/automations/runtime, gmail + fs via
# app/agents/agent_triggers. Each WAKES the agent for real — no inert agents.
VALID_TRIGGER_TYPES = {
    "cron",
    "gmail_any_new", "gmail_new_from_sender", "gmail_keyword_match",
    "fs_new_in_folder",
    "whatsapp_group_new", "whatsapp_keyword_match",
}

# Recognized by the automation layer but NOT wired for agents yet.
DEFERRED_TRIGGER_TYPES = {
    "whatsapp_outgoing_new", "whatsapp_smart_reply",
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
    "\"triggers\": [{\"trigger_type\": \"<type>\", \"trigger_config\": {...}}]}\n\n"
    "CHOOSE THE TRIGGER TYPE THAT MATCHES THE EVENT. Do NOT default to cron for "
    "event-driven tasks. Use cron ONLY for genuinely time-scheduled work.\n"
    "Trigger types and when to use each:\n"
    "- gmail_any_new — 'when any email/mail arrives', 'whenever I get an email'. "
    "config: {} (empty).\n"
    "- gmail_new_from_sender — 'when email from <person/address> arrives'. "
    "config: {\"sender\": \"<email address>\"}.\n"
    "- gmail_keyword_match — 'when an email about <topic> arrives'. "
    "config: {\"keywords\": \"<Gmail search query, e.g. 'invoice OR payment'>\"}.\n"
    "- fs_new_in_folder — 'watch a folder', 'when a new file appears in <folder>'. "
    "config: {\"folder\": \"<path>\", \"file_extensions\": [\"pdf\", ...]}  (file_extensions optional; [] = all).\n"
    "- whatsapp_group_new — 'when a message arrives in a WhatsApp group'. "
    "config: {\"chat_id\": \"<group id ending @g.us, or '' for any>\"}.\n"
    "- whatsapp_keyword_match — 'when a WhatsApp message mentions <X>'. "
    "config: {\"keywords\": \"<space/comma separated>\"}.\n"
    "- cron — recurring on a SCHEDULE ('every morning', 'every 2 minutes', 'daily at 9'). "
    "config: {\"cron\": \"<5-field cron expression>\"}.\n\n"
    "Examples:\n"
    "- 'message me on Telegram when any mail comes' -> gmail_any_new, config {}.\n"
    "- 'watch my downloads folder and summarize new PDFs' -> fs_new_in_folder, "
    "config {\"folder\": \"downloads\", \"file_extensions\": [\"pdf\"]}.\n"
    "- 'send hello every 2 minutes' -> cron, config {\"cron\": \"*/2 * * * *\"}.\n\n"
    f"Allowed trigger_type values (use ONLY these): {sorted(VALID_TRIGGER_TYPES)}.\n"
    "An agent may have multiple triggers. Never invent a trigger_type outside the allowed set. "
    "If the request truly has no event/schedule, pick the closest supported trigger and note it in role_block."
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
            if len((cfg.get("cron", "")).split()) != 5:
                raise ValueError(f"Invalid cron expression {cfg.get('cron')!r}")
        elif tt == "fs_new_in_folder":
            if not cfg.get("folder"):
                raise ValueError("fs_new_in_folder requires a non-empty 'folder'")
        elif tt == "gmail_new_from_sender":
            if not cfg.get("sender"):
                raise ValueError("gmail_new_from_sender requires a 'sender'")
        elif tt == "gmail_keyword_match":
            if not cfg.get("keywords"):
                raise ValueError("gmail_keyword_match requires 'keywords'")


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
