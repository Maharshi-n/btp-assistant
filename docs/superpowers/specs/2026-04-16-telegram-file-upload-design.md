# Telegram File Upload — Design Spec

**Date:** 2026-04-16  
**Status:** Approved  
**Feature:** Accept files sent via Telegram, save to workspace, run agent with full context

---

## Overview

Users can send files to RAION via Telegram. A file is always in service of what the user said in text — the two together form one intent. The webhook is extended to handle this naturally without breaking the existing text/automation flow.

The feature is a **bypass/cache layer**: the webhook downloads the file, saves it to a fixed folder (`workspace/telegram_uploads/`), and hands it off to the agent with the user's intent text. The agent decides what to do from there. RAION does not pre-analyse or restrict file types at the webhook level — if a user asks the agent to read a video or voice note, the agent handles the sorry message itself.

---

## Data Model

### New table: `TelegramPendingFile`

Stores a text intent while the user has not yet sent the accompanying file.

```
TelegramPendingFile
  id              int  PK autoincrement
  chat_id         str  NOT NULL  (Telegram chat ID, unique per chat)
  intent_text     str  NOT NULL  (the user's instruction, e.g. "save this in reports/")
  thread_id       int  nullable  (active DB thread to post into)
  conversation_id int  nullable  (active AutomationConversation, if any)
  created_at      datetime
```

- One row per `chat_id` — upsert on insert (old intent replaced by new one).
- No expiry — waits indefinitely until a file or cancelling text arrives.
- Cleared immediately when a file is received or the user redirects.

The existing `TelegramPendingReply` table is **untouched**.

---

## Webhook Flow

A Telegram message can contain text, a file, or both (file + caption). The webhook handles these as one unified flow:

```
Incoming message
│
├── has file (document / photo / audio / video / voice)
│   ├── intent = caption text  OR  stored TelegramPendingFile.intent_text  OR  generic fallback
│   ├── download file via Telegram getFile API → save to workspace/telegram_uploads/<filename>
│   ├── clear TelegramPendingFile for this chat_id (if any)
│   └── run agent normally with prompt:
│         "[intent_text]\n\nFile saved to: workspace/telegram_uploads/<filename>"
│
└── text only
    ├── existing command handling (/newthread, /thread) — unchanged, checked first
    ├── existing TelegramPendingReply lookup — unchanged, checked second
    ├── TelegramPendingFile exists for this chat_id
    │   → clear it, treat new text as normal (re-check heuristic or run agent)
    ├── text matches file-incoming heuristic
    │   → store TelegramPendingFile, reply "Got it, send the file when ready."
    └── otherwise → normal agent run (no file involved)
```

### File Download Steps

1. Extract `file_id` from `message.document` / `message.photo[-1]` / `message.audio` / `message.voice` / `message.video`
2. `GET https://api.telegram.org/bot{token}/getFile?file_id={file_id}` → get `file_path`
3. Download from `https://api.telegram.org/file/bot{token}/{file_path}`
4. Save to `workspace/telegram_uploads/{original_filename}` — fall back to `{file_id}.bin` if no name available
5. Create `workspace/telegram_uploads/` if it doesn't exist

### Agent Prompt Construction

```
{intent_text}

File saved to: workspace/telegram_uploads/{filename}
```

Generic fallback intent (no caption, no stored pending):
```
User sent a file via Telegram. It has been saved to: workspace/telegram_uploads/{filename}. What should I do with it?
```

---

## File-Incoming Heuristic

Keyword/phrase matching on incoming text (case-insensitive, no LLM call):

**Trigger words:** `upload`, `uploading`, `sending`, `will send`, `attaching`, `file`, `document`, `pdf`, `image`, `photo`, `here is`, `here's`, `check this`

If any trigger word is present in the text → store as `TelegramPendingFile`, reply "Got it, send the file when ready."

If a `TelegramPendingFile` already exists and new text arrives → clear it, re-evaluate the new text through the same heuristic (may create a new pending or run agent directly).

---

## Upload Folder

**Fixed path:** `workspace/telegram_uploads/`

- Created automatically on first use.
- Acts as RAION's Telegram bypass/cache folder.
- The agent can reference files here via the normal `read_file`, `write_file`, `list_dir` tools.
- No automatic cleanup — user or agent manages the folder contents.

---

## Telegram Replies

| Situation | Reply |
|-----------|-------|
| Text stored as pending file intent | "Got it, send the file when ready." |
| File received (any case) | "Got it, working on it..." → agent runs → result sent back |
| Agent produces a result | Result text sent, then "Anything else?" |

---

## What Is NOT Changed

- `TelegramPendingReply` table and all automation resume logic — untouched
- `/newthread`, `/thread` commands — untouched
- End-of-conversation phrase detection — untouched
- `telegram_send`, `telegram_ask`, `telegram_send_file`, `save_draft` tools — untouched
- File type restriction — none. Agent handles unsupported formats with a natural sorry message.

---

## Files to Change / Create

| File | Change |
|------|--------|
| `app/db/models.py` | Add `TelegramPendingFile` model |
| `app/db/engine.py` | Table created via existing `Base.metadata.create_all` on startup |
| `app/web/routes/telegram.py` | Add file detection, download, pending file logic |
| `app/web/routes/settings.py` | No change needed |

No new routes, no new tools, no new config vars needed.
