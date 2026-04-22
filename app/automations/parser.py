"""Phase 9: Natural-language automation parser.

One OpenAI structured-output call converts an NL description into a
parsed automation spec:
  {
    "name": "<short human name>",
    "trigger_type": "cron" | "gmail_new_from_sender" | "fs_new_in_folder"
                  | "whatsapp_group_new" | "whatsapp_keyword_match",
    "trigger_config": { ... },   # depends on trigger_type
    "action_prompt": "<what the supervisor should do when triggered>"
  }

trigger_config schemas:
  cron              → {"cron": "<cron expression, e.g. '*/1 * * * *'>"}
  gmail_new_from_sender → {"sender": "<email address>"}
  fs_new_in_folder  → {"folder": "<absolute or workspace-relative path>"}
  whatsapp_group_new    → {"chat_id": "<group chat_id ending in @g.us, or '' for any group>"}
  whatsapp_keyword_match → {"keywords": "<space/comma separated keywords>"}
"""
from __future__ import annotations

import json
from typing import Optional

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.db.models import Skill, UserMemory

_SYSTEM_PROMPT = """\
Convert a natural-language automation description into a JSON object.
Output ONLY the JSON — no explanation, no markdown fences.

━━━ JSON SCHEMA ━━━
{{
  "name":           "<short human-readable name, 3-6 words>",
  "trigger_type":   "cron" | "gmail_any_new" | "gmail_new_from_sender" | "gmail_keyword_match" | "fs_new_in_folder" | "whatsapp_group_new" | "whatsapp_keyword_match" | "whatsapp_outgoing_new" | "whatsapp_smart_reply",
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

gmail_any_new:
  {{}}   (empty config — fires on ANY new email in inbox)
  Use when user says "any email", "every email", "whenever I get an email", "all emails"

gmail_new_from_sender:
  {{"sender": "<email address>"}}
  Use only when user specifies a particular sender address or person

gmail_keyword_match:
  {{"keywords": "<Gmail search query, e.g. 'hackathon OR fest OR competition'>"}}
  Rules:
  - Use Gmail search syntax: OR, AND, subject:, from:, etc.
  - "hackathon" → {{"keywords": "hackathon"}}
  - "hackathon or coding contest" → {{"keywords": "hackathon OR coding contest"}}
  - "mail about fees from college" → {{"keywords": "fees subject:fees OR fee"}}
  - "important mail from HR" → {{"keywords": "from:hr subject:important OR urgent"}}

fs_new_in_folder:
  {{"folder": "<absolute path>"}}
  - If user says a relative path or folder name, prefix with: {workspace}
  - e.g. "inbox" → "{workspace}/inbox"
  - e.g. "mndc_mails" → "{workspace}/mndc_mails"

whatsapp_group_new:
  {{"chat_id": "<group chat_id ending in @g.us, or empty string for any monitored group>"}}
  Use when: "when a WhatsApp group message arrives", "monitor my WhatsApp group", "on new WhatsApp message"
  - If user names a specific group, keep chat_id empty (the runtime will pass the real chat_id in TRUSTED TRIGGER CONTEXT)
  - If user says "any group" or "all groups", set chat_id to ""
  - IMPORTANT: When the action is to reply in the same group, the action_prompt MUST say:
    "Read the TRUSTED TRIGGER CONTEXT block. Use chat_id from that block as the first argument to whatsapp_send."
    Do NOT tell the supervisor to call whatsapp_get_groups — the chat_id is already in TRUSTED TRIGGER CONTEXT.

whatsapp_keyword_match:
  {{"keywords": "<space or comma separated keywords>"}}
  Use when: "when someone mentions X in WhatsApp", "if a WhatsApp message contains Y"
  - Keywords are matched case-insensitively against incoming message text
  - "urgent or emergency" → {{"keywords": "urgent emergency"}}
  - "meeting or standup" → {{"keywords": "meeting standup"}}

whatsapp_outgoing_new:
  {{"chat_id": "<group chat_id ending in @g.us, or empty string for any group>"}}
  Use when: "when I send a WhatsApp message", "when RAION sends to WhatsApp", "log my outgoing WhatsApp messages"
  - chat_id="" means fire on any outgoing message to any group
  - Specific chat_id restricts to that group only

whatsapp_smart_reply:
  {{"chat_id": "<group chat_id or empty for any>", "topic_description": "<detailed description of what messages to match>", "reply_context": "<what to reply or context for the reply>"}}
  Use when: "auto-reply", "if someone asks about X reply with Y", "if message is about <topic> respond with <answer>"
  Rules:
  - topic_description must be detailed — it's passed to an LLM for semantic matching
  - reply_context is the answer/context to use in the reply — can be a full paragraph
  - Example: user says "if someone asks about hackathon registration in Campaign Group, reply with: Registration closes April 30. Form: https://forms.gle/xyz"
    → {{"chat_id": "120363406807283271@g.us", "topic_description": "questions about hackathon registration, deadline, how to register, registration form", "reply_context": "Registration closes April 30. Form: https://forms.gle/xyz"}}
  - If user doesn't specify a group, set chat_id to ""
  - For the action_prompt: "A WhatsApp message matched your smart reply rule. The reply has already been sent directly."

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

FOR FS_NEW_IN_FOLDER + telegram_ask:
A "TRUSTED TRIGGER CONTEXT" block is injected at runtime containing conversation_id and file_path.
Every telegram_ask MUST carry conversation_id. Reference file_path from the TRUSTED block — never say "the file".

CORRECT action_prompt pattern for fs_new_in_folder + telegram_ask:
  "Read the TRUSTED TRIGGER CONTEXT block above. Note the conversation_id and file_path.
   Read the file at file_path. Summarize its contents in 2 sentences.
   Call telegram_ask with:
     question: '[filename]: [summary]\n\nWhat should I do with this file?'
     continuation_prompt: 'The user wants to: [their reply]. The file is at <file_path from
       TRUSTED TRIGGER CONTEXT block>. Carry out the user's instruction on that file.
       If you need to show the user something for approval, call save_draft(conversation_id=<id>,
       draft=<content>) then call telegram_ask again with the same conversation_id.'
     conversation_id: <the id from the TRUSTED block>"

RULE: Every telegram_ask in every round MUST carry conversation_id so the webhook
injects the real file_path into the next round's prompt automatically.

FOR WHATSAPP TRIGGERS — a "TRUSTED TRIGGER CONTEXT" block is injected at runtime containing:
chat_id, sender_id, sender_name, message_text, group_name (for group triggers).
Do NOT tell the assistant to fetch the message — it already has it. Focus on what to DO with it:
  BAD : "Read the WhatsApp message and process it"
  GOOD: "Read the TRUSTED TRIGGER CONTEXT block above. Note the chat_id, sender_name, and message_text.
         Summarize the message in one sentence and call telegram_send with that summary plus the sender name."

  BAD : "Reply to the WhatsApp message"
  GOOD: "Read the TRUSTED TRIGGER CONTEXT block above. Note the message_text, sender_name, and chat_id.
         Call whatsapp_send with chat_id from the TRUSTED block (exactly as shown) and an appropriate reply message."

  RULE for reply automations: ALWAYS use chat_id from TRUSTED TRIGGER CONTEXT — never call whatsapp_get_groups
  to find it. The chat_id is already provided at trigger time.

FOR CRON WHATSAPP SUMMARY AUTOMATIONS — use whatsapp_fetch_messages (NOT whatsapp_read_messages):
  whatsapp_fetch_messages reads from RAION's local database. It is the correct tool for:
  - Periodic summaries ("every 3 hours summarize all groups")
  - Daily/weekly reports ("today's messages from all groups")
  - Any automation that aggregates messages over a time window

  Parameters:
  - chat_id: "" for all groups, or specific chat_id for one group
  - hours_back: number of hours to look back (e.g. 3.0 for 3-hour windows)
  - since_midnight: true for "today" queries
  - limit: max messages (default 500 is fine for most cases)

  The tool returns "No messages in this period" when the window is empty — automations
  should check for this and skip writing logs if there is nothing to report.

  GOOD action_prompt for a 3-hour summary automation:
  "Call whatsapp_fetch_messages with chat_id='' and hours_back=3.
   If the result says 'No messages', stop — do not write a log.
   Otherwise, write a concise group-by-group summary (group name, message count, key topics/updates)
   and append it to whatsapp_logs/summary_<YYYY-MM-DD>.txt in my workspace with a timestamp header.
   Then call telegram_send with a brief digest of the most important updates across all groups."

━━━ TELEGRAM NOTIFICATIONS ━━━
Two tools are available for Telegram: telegram_send and telegram_ask.

telegram_send → one-way notification. Use when no reply is needed.
telegram_ask  → two-way. Use when the automation needs the user's input before continuing
                (e.g. "ask me what to reply", "show me the draft first", "ask for my approval").

CHOOSING WHICH TO USE:
- "notify me", "send me a summary", "ping me" → telegram_send
- "ask me for a reply", "let me review", "ask me before sending", "show me the draft" → telegram_ask

FOR telegram_ask, the action_prompt must:
1. Read the sender email address and subject from the email header shown above the prompt.
2. Summarize the trigger content (email body, file contents, etc.)
3. Call telegram_ask with:
   - question: the summary + what you're asking the user
   - continuation_prompt: FULL self-contained instructions, with the REAL sender email and subject
     substituted in — NOT placeholders like <SENDER_EMAIL>.

━━━ GMAIL TRIGGER — USE TRUSTED CONTEXT ━━━
For gmail triggers, a "TRUSTED TRIGGER CONTEXT" block is injected at runtime containing:
conversation_id, recipient_email, email_subject, email_body.

Your action_prompt MUST:
1. Reference recipient_email from the TRUSTED block — do NOT hardcode it
2. Pass conversation_id={conversation_id} to EVERY telegram_ask call so the
   webhook can inject sender/draft/subject automatically in each round
3. When calling gmail_send, use recipient_email from the TRUSTED block exactly —
   never a placeholder, never example.com, never a hallucination
4. Before showing a draft via telegram_ask, call save_draft(conversation_id=<id>, draft=<full text>)
   so the next round can retrieve it from the TRUSTED CONVERSATION CONTEXT block

CORRECT action_prompt pattern for gmail + telegram_ask:
  "Read the TRUSTED TRIGGER CONTEXT block above. Note the conversation_id and recipient_email.
   Summarize email_body in 2 sentences.
   Call telegram_ask with:
     question: '[summary]\n\nWhat should I reply? Give me your key points.'
     continuation_prompt: 'Write a professional draft reply based on the user intent after
       User reply:. Sign as Maharshi. Then call save_draft(conversation_id=<id from TRUSTED
       CONVERSATION CONTEXT>, draft=<full draft text>). Then call telegram_ask with
       question=\"Here is the draft reply:\n\n[full draft text]\n\nType OK to send, or tell me
       what to change.\" and continuation_prompt=\"If user approved: call gmail_send with
       to=<recipient_email from TRUSTED CONVERSATION CONTEXT block>,
       subject=<email_subject from that block>, body=<current_draft from that block>.
       If user wants changes: rewrite draft, call save_draft again, then call telegram_ask again.\"
       and conversation_id=<same id>.'
     conversation_id: <the id from the TRUSTED block>"

RULE: Every telegram_ask call in every round MUST carry conversation_id.
This makes the webhook inject the real sender/subject/draft into the next round's prompt —
you do NOT need to remember them.

━━━ MULTI-ROUND CONVERSATIONS ━━━
telegram_ask can chain into another telegram_ask inside the continuation_prompt.
This enables multi-round flows: ask → draft → show draft → get approval → send.

CRITICAL for continuation_prompt:
- NEVER hardcode email addresses — use recipient_email from the TRUSTED CONVERSATION CONTEXT block
- NEVER say "I will call telegram_ask" — actually call it as a tool
- ALWAYS pass conversation_id on every telegram_ask call
- ALWAYS call save_draft before showing a draft for approval

Do NOT use telegram_ask if the user did not mention asking/reviewing/approving.
Do NOT use telegram_send at all if the user did not mention notify/send me/ping me.

Output ONLY the JSON object."""


async def _build_context_block(db: Optional[AsyncSession]) -> str:
    """Fetch memories + enabled skills from the DB and format them for the parser prompt.

    Returns empty string if db is None or there is nothing to inject.
    """
    if db is None:
        return ""

    try:
        mem_result = await db.execute(select(UserMemory).order_by(UserMemory.created_at.desc()))
        memories = mem_result.scalars().all()
    except Exception:
        memories = []

    try:
        skill_result = await db.execute(
            select(Skill).where(Skill.enabled == True).order_by(Skill.name)
        )
        skills = skill_result.scalars().all()
    except Exception:
        skills = []

    memory_block = "\n".join(f"- {m.content}" for m in memories) or "(none)"
    skill_block = "\n".join(f"- /{s.name}: {s.trigger_description}" for s in skills) or "(none)"

    return (
        "━━━ USER MEMORIES (resolve names/teams/places to concrete values) ━━━\n"
        f"{memory_block}\n\n"
        "━━━ AVAILABLE SKILLS (reusable procedures the runtime agent can load) ━━━\n"
        f"{skill_block}\n\n"
        "RULES:\n"
        "- If the description references a person/team (e.g. \"my ML team\"), look up USER MEMORIES "
        "and include the resolved email addresses in the action_prompt. Keep the original name too so "
        "the runtime can re-check memory if addresses look stale. Example:\n"
        "  \"Send summary to my ML team (Sanat <sanat@x.com>, Vidhansh <vid@x.com>). "
        "If these look outdated, call search_memory for 'ML team' before sending.\"\n"
        "- NEVER invent placeholder emails like ml_team@example.com. If a name can't be resolved from "
        "memory, keep the raw name in the action_prompt so the runtime agent can ask memory tools.\n"
        "- If a skill in AVAILABLE SKILLS matches the task, reference it in the action_prompt like:\n"
        "  \"Load the /<skill_name> skill and follow its steps.\"\n"
        "  The runtime agent will call read_skill to pull the full procedure.\n"
        "- Only reference skills that appear in the list — never invent skill names.\n\n"
    )


async def parse_automation(nl_description: str, db: Optional[AsyncSession] = None) -> dict:
    """Call OpenAI to parse *nl_description* into a structured automation spec.

    If *db* is provided, user memories and enabled skills are injected into the
    prompt so the parser can resolve names (e.g. "my ML team") to real emails
    and reference existing skill procedures.

    Returns a dict with keys: name, trigger_type, trigger_config, action_prompt.
    Raises ValueError if the response cannot be parsed.
    """
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)

    system = _SYSTEM_PROMPT.replace("{workspace}", str(app_config.WORKSPACE_DIR))
    context_block = await _build_context_block(db)
    user_content = f"{context_block}AUTOMATION DESCRIPTION:\n{nl_description}" if context_block else nl_description

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
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

    valid_types = {"cron", "gmail_any_new", "gmail_new_from_sender", "gmail_keyword_match", "fs_new_in_folder", "whatsapp_group_new", "whatsapp_keyword_match", "whatsapp_outgoing_new", "whatsapp_smart_reply"}
    if parsed["trigger_type"] not in valid_types:
        raise ValueError(
            f"Invalid trigger_type {parsed['trigger_type']!r}. Must be one of {valid_types}"
        )

    return parsed
