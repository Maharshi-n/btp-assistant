"""Phase 9: Natural-language automation parser.

One OpenAI structured-output call converts an NL description into a
parsed automation spec:
  {
    "name": "<short human name>",
    "trigger_type": "cron" | "gmail_new_from_sender" | "fs_new_in_folder",
    "trigger_config": { ... },   # depends on trigger_type
    "action_prompt": "<what the supervisor should do when triggered>"
  }

trigger_config schemas:
  cron              → {"cron": "<cron expression, e.g. '*/1 * * * *'>"}
  gmail_new_from_sender → {"sender": "<email address>"}
  fs_new_in_folder  → {"folder": "<absolute or workspace-relative path>"}
"""
from __future__ import annotations

import json

from openai import AsyncOpenAI

import app.config as app_config

_SYSTEM_PROMPT = """\
Convert a natural-language automation description into a JSON object.
Output ONLY the JSON — no explanation, no markdown fences.

━━━ JSON SCHEMA ━━━
{{
  "name":           "<short human-readable name, 3-6 words>",
  "trigger_type":   "cron" | "gmail_new_from_sender" | "fs_new_in_folder",
  "trigger_config": {{ ... }},
  "action_prompt":  "<instruction for the AI assistant>"
}}

━━━ TRIGGER CONFIG ━━━
cron:
  {{"cron": "<5-field cron expression>"}}
  Rules:
  - Minimum interval is 1 minute. Sub-minute requests → "*/1 * * * *"
  - "every minute"        → "*/1 * * * *"
  - "every 2 minutes"     → "*/2 * * * *"
  - "every 5 minutes"     → "*/5 * * * *"
  - "every 30 minutes"    → "*/30 * * * *"
  - "every hour"          → "0 * * * *"
  - "every day at 8am"    → "0 8 * * *"
  - "every day at 9:30am" → "30 9 * * *"
  - "every monday at 9am" → "0 9 * * 1"
  - "every weekday at 6pm"→ "0 18 * * 1-5"

gmail_new_from_sender:
  {{"sender": "<email address>"}}

fs_new_in_folder:
  {{"folder": "<absolute path>"}}
  - If user says a relative path or folder name, prefix with: {workspace}
  - e.g. "inbox" → "{workspace}/inbox"
  - e.g. "mndc_mails" → "{workspace}/mndc_mails"

━━━ ACTION PROMPT RULES ━━━
Write action_prompt as a clear, direct instruction to an AI assistant.
It must be specific enough that the assistant can act without asking questions.

NEVER include:
  - Absolute file paths (use "in my workspace" instead)
  - Gmail message IDs or raw trigger metadata
  - Vague phrases like "handle it" or "process the trigger"

FOR CRON TRIGGERS — be explicit about exactly what to do each time it fires:
  BAD : "Write the time to a file"
  GOOD: "Get the current date and time in IST and write it to heartbeat.txt in my workspace, overwriting any previous content."

  BAD : "Check emails and summarize"
  GOOD: "List my 5 most recent unread emails, summarize each one in 2 sentences, and append the summaries to daily_digest.txt in my workspace with today's date as a header."

  BAD : "Save news to file"
  GOOD: "Search the web for today's top 5 technology news headlines, write a concise summary of each, and save them to tech_news.txt in my workspace with today's IST date as the title."

FOR GMAIL TRIGGERS — the full email content is injected automatically before this prompt at runtime.
Do NOT tell the assistant to fetch or read the email — it already has it. Focus on what to DO with it:
  BAD : "Read the email from the sender and reply to it"
  BAD : "Process the new email"
  GOOD: "Read the email above carefully. Draft a polite, professional reply addressing all points raised. Send the reply using gmail_send."

  BAD : "Summarize the email"
  GOOD: "Read the email above. Write a 3-sentence summary covering: who sent it, what they want, and any action required. Save it to email_summaries.txt in my workspace, appending with today's date."

FOR FS_NEW_IN_FOLDER TRIGGERS — the exact file path is injected automatically at runtime.
Do NOT tell the assistant to look for a file — it has the path. Focus on what to do with it:
  BAD : "Analyze the new file"
  GOOD: "Read the new file at the path shown above. Analyze its contents and write a structured report covering: key topics, important data points, and a 2-sentence conclusion. Save the report as report_<original_filename>.txt in my workspace."

  BAD : "Process the file"
  GOOD: "Read the new file shown above and translate its entire contents to Hindi. Save the translation to the same filename with _hindi appended, inside my workspace."

━━━ TELEGRAM NOTIFICATIONS ━━━
If the user's description mentions notifying, alerting, pinging, or sending a notification,
append the following sentence to the end of action_prompt (adapt the italicised part to context):

For email triggers  → "Then call telegram_send with a 2-sentence summary: who sent the email and what action was taken."
For cron triggers   → "Then call telegram_send with a 2-sentence summary of what was done and the current IST time."
For file triggers   → "Then call telegram_send with a 1-sentence note: what file arrived and what was done with it."

Keep the telegram_send instruction as the LAST sentence of action_prompt.
Do NOT add it if the user did not mention notification/alert/notify/ping.

Output ONLY the JSON object."""


async def parse_automation(nl_description: str) -> dict:
    """Call OpenAI to parse *nl_description* into a structured automation spec.

    Returns a dict with keys: name, trigger_type, trigger_config, action_prompt.
    Raises ValueError if the response cannot be parsed.
    """
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)

    system = _SYSTEM_PROMPT.replace("{workspace}", str(app_config.WORKSPACE_DIR))

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": nl_description},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"OpenAI returned non-JSON: {raw!r}") from exc

    # Validate required keys
    required = {"name", "trigger_type", "trigger_config", "action_prompt"}
    missing = required - parsed.keys()
    if missing:
        raise ValueError(f"Parsed automation missing keys: {missing}. Got: {parsed}")

    valid_types = {"cron", "gmail_new_from_sender", "fs_new_in_folder"}
    if parsed["trigger_type"] not in valid_types:
        raise ValueError(
            f"Invalid trigger_type {parsed['trigger_type']!r}. Must be one of {valid_types}"
        )

    return parsed
