"""Phase 8: Gmail, Drive, and Calendar tools.

All tools load credentials from the `oauth_tokens` table (Fernet-encrypted).
If no token is stored, they raise a descriptive error telling the user to connect.

Policy (from plan):
  - gmail_list_unread, gmail_read, gmail_search  → auto   (read-only)
  - gmail_send                                    → ask    (network write)
  - drive_list, drive_read                        → auto   (read-only)
  - drive_write                                   → ask    (network write)
  - calendar_list_events                          → auto   (read-only)
  - calendar_create_event                         → ask    (network write)
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
import mimetypes
import os
from typing import Any

from langchain_core.tools import tool


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

_NOT_CONNECTED_MSG = (
    "Google account not connected. Go to /settings and click 'Connect Google'."
)

# Strings that indicate a file contains placeholder/template content rather
# than real data. Checked against the first 512 bytes (lowercased) of any
# file before it is uploaded to Drive. Add new patterns here as needed.
_DRIVE_UPLOAD_PLACEHOLDER_MARKERS = [
    "<place your content here>",
    "<your content here>",
    "[content goes here]",
    "[placeholder]",
]


def _load_credentials():
    """Synchronously load and return google.oauth2.credentials.Credentials.

    Raises RuntimeError with a user-friendly message if not connected.
    """
    import app.config as app_config

    if not app_config.FERNET_KEY:
        raise RuntimeError(
            "FERNET_KEY not set in .env — cannot decrypt OAuth tokens. "
            "Generate one and add it to .env."
        )

    # Import here to avoid hard dependency at module load time (before install)
    from cryptography.fernet import Fernet
    from google.oauth2.credentials import Credentials

    from sqlalchemy import select as sa_select
    from app.db.engine import SyncSessionLocal
    from app.db.models import OAuthToken

    try:
        with SyncSessionLocal() as db:
            token_json = db.execute(
                sa_select(OAuthToken.token_json).where(OAuthToken.provider == "google").limit(1)
            ).scalar_one_or_none()
    except Exception as exc:
        raise RuntimeError(f"Failed to read OAuth token from DB: {exc}") from exc

    if token_json is None:
        raise RuntimeError(_NOT_CONNECTED_MSG)

    fernet = Fernet(app_config.FERNET_KEY.encode())
    try:
        decrypted = fernet.decrypt(token_json.encode()).decode()
        token_data: dict = json.loads(decrypted)
    except Exception as exc:
        raise RuntimeError(f"Failed to decrypt OAuth token: {exc}") from exc

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    return creds


def _refresh_if_needed(creds) -> None:
    """Refresh expired credentials and persist the new token to DB."""
    import app.config as app_config
    from cryptography.fernet import Fernet

    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or []),
        }
        fernet = Fernet(app_config.FERNET_KEY.encode())
        encrypted = fernet.encrypt(json.dumps(token_data).encode()).decode()
        now = datetime.now(timezone.utc)

        from sqlalchemy import select as sa_select
        from app.db.engine import SyncSessionLocal
        from app.db.models import OAuthToken

        with SyncSessionLocal() as db:
            row = db.execute(
                sa_select(OAuthToken).where(OAuthToken.provider == "google").limit(1)
            ).scalar_one_or_none()
            if row:
                row.token_json = encrypted
                row.refreshed_at = now
                db.commit()


def _get_gmail_service():
    from googleapiclient.discovery import build
    creds = _load_credentials()
    _refresh_if_needed(creds)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _get_drive_service():
    from googleapiclient.discovery import build
    creds = _load_credentials()
    _refresh_if_needed(creds)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _get_calendar_service():
    from googleapiclient.discovery import build
    creds = _load_credentials()
    _refresh_if_needed(creds)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Helper: decode email body
# ---------------------------------------------------------------------------

def _decode_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    elif mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            result = _decode_body(part)
            if result:
                return result
    return ""


# ---------------------------------------------------------------------------
# Gmail tools
# ---------------------------------------------------------------------------

@tool
def gmail_list_unread(max_results: int = 10) -> str:
    """List the most recent unread Gmail messages.

    Args:
        max_results: Maximum number of messages to return (default 10, max 50).

    Returns:
        JSON list of {id, subject, from, date, snippet}.
    """
    max_results = min(int(max_results), 50)
    try:
        service = _get_gmail_service()
        resp = service.users().messages().list(
            userId="me",
            q="is:unread",
            maxResults=max_results,
        ).execute()

        message_refs = resp.get("messages", [])
        if not message_refs:
            return json.dumps([])

        results = []
        for ref in message_refs:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            results.append({
                "id": ref["id"],
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            })

        return json.dumps(results, ensure_ascii=False)
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Gmail error: {exc}"


@tool
def gmail_read(message_id: str) -> str:
    """Read the full content of a Gmail message by its ID.

    Args:
        message_id: The Gmail message ID (from gmail_list_unread or gmail_search).

    Returns:
        JSON with {id, subject, from, date, body}.
    """
    try:
        service = _get_gmail_service()
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        body = _decode_body(payload)
        return json.dumps({
            "id": message_id,
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "body": body[:8000],  # cap at 8 KB
        }, ensure_ascii=False)
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Gmail error: {exc}"


@tool
def gmail_search(query: str, max_results: int = 10) -> str:
    """Search Gmail messages using Gmail search syntax.

    Args:
        query: Gmail search query (e.g. 'from:boss@example.com subject:report').
        max_results: Maximum number of results (default 10, max 50).

    Returns:
        JSON list of {id, subject, from, date, snippet}.
    """
    max_results = min(int(max_results), 50)
    try:
        service = _get_gmail_service()
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        message_refs = resp.get("messages", [])
        if not message_refs:
            return json.dumps([])

        results = []
        for ref in message_refs:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            results.append({
                "id": ref["id"],
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            })

        return json.dumps(results, ensure_ascii=False)
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Gmail search error: {exc}"


@tool
def gmail_send(to: str, subject: str, body: str, attachments: list[str] | None = None) -> str:
    """Send an email via Gmail, optionally with file attachments.

    This action requires user approval before sending.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        attachments: Optional list of absolute file paths to attach directly to
            the email. Files can be anywhere the process can read (no workspace
            sandbox) — this is the ATTACHMENT path, not an upload. Use this
            instead of uploading to Drive when the user says "attach".

    Returns:
        Confirmation with the sent message ID, or an error string.
    """
    try:
        service = _get_gmail_service()

        if not attachments:
            mime_msg: Any = MIMEText(body)
        else:
            mime_msg = MIMEMultipart()
            mime_msg.attach(MIMEText(body, "plain"))
            for path in attachments:
                if not os.path.isfile(path):
                    return f"Gmail send error: attachment not found: {path}"
                ctype, encoding = mimetypes.guess_type(path)
                if ctype is None or encoding is not None:
                    ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)
                with open(path, "rb") as fh:
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(fh.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=os.path.basename(path),
                )
                mime_msg.attach(part)

        mime_msg["to"] = to
        mime_msg["subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        sent = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        return json.dumps({
            "status": "sent",
            "id": sent.get("id", ""),
            "attachments": len(attachments or []),
        })
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Gmail send error: {exc}"


# ---------------------------------------------------------------------------
# Drive tools
# ---------------------------------------------------------------------------

@tool
def drive_list(folder_id: str = "root", max_results: int = 20) -> str:
    """List files in a Google Drive folder.

    Args:
        folder_id: Drive folder ID (default "root" for My Drive).
        max_results: Maximum number of files to return (default 20, max 100).

    Returns:
        JSON list of {id, name, mimeType, modifiedTime}.
    """
    max_results = min(int(max_results), 100)
    try:
        service = _get_drive_service()
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=max_results,
            fields="files(id,name,mimeType,modifiedTime)",
        ).execute()
        return json.dumps(resp.get("files", []), ensure_ascii=False)
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Drive list error: {exc}"


@tool
def drive_read(file_id: str) -> str:
    """Read the content of a plain-text or Google Docs file from Drive.

    For Google Docs, exports as plain text. For other files, reads raw bytes
    (capped at 64 KB).

    Args:
        file_id: Drive file ID.

    Returns:
        The file content as a string, or an error message.
    """
    try:
        service = _get_drive_service()
        meta = service.files().get(fileId=file_id, fields="mimeType,name").execute()
        mime_type: str = meta.get("mimeType", "")

        if mime_type == "application/vnd.google-apps.document":
            content_bytes = service.files().export(
                fileId=file_id, mimeType="text/plain"
            ).execute()
        else:
            import io
            from googleapiclient.http import MediaIoBaseDownload
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=file_id))
            done = False
            while not done:
                _, done = downloader.next_chunk()
            content_bytes = buf.getvalue()

        text = (
            content_bytes.decode("utf-8", errors="replace")
            if isinstance(content_bytes, bytes)
            else str(content_bytes)
        )
        return text[:65536]  # cap at 64 KB
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Drive read error: {exc}"


@tool
def drive_write(folder_id: str, name: str, content: str) -> str:
    """Create or update a plain-text file in Google Drive.

    This action requires user approval before writing.

    Args:
        folder_id: ID of the parent Drive folder (use "root" for My Drive root).
        name: File name (including extension, e.g. "notes.txt").
        content: Plain-text content to write.

    Returns:
        JSON with {id, name, webViewLink} on success, or an error string.
    """
    try:
        import io

        from googleapiclient.http import MediaIoBaseUpload

        service = _get_drive_service()

        # Check if a file with this name already exists in the folder
        resp = service.files().list(
            q=f"'{folder_id}' in parents and name = '{name}' and trashed = false",
            fields="files(id)",
        ).execute()
        existing = resp.get("files", [])

        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")),
            mimetype="text/plain",
            resumable=False,
        )

        if existing:
            file_id = existing[0]["id"]
            updated = service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id,name,webViewLink",
            ).execute()
            return json.dumps(updated, ensure_ascii=False)
        else:
            created = service.files().create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id,name,webViewLink",
            ).execute()
            return json.dumps(created, ensure_ascii=False)
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Drive write error: {exc}"


@tool
def drive_download(file_id: str, save_path: str = "") -> str:
    """Download any file from Google Drive and save it to the workspace.

    IMPORTANT: Always call drive_list first to get the correct file_id.
    Never guess or fabricate a file_id.

    Works with all file types: PDFs, images, Office docs, etc.
    Google Docs → exported as .docx
    Google Sheets → exported as .xlsx
    Google Slides → exported as .pptx

    Args:
        file_id:   Drive file ID — get this from drive_list, never guess it.
        save_path: Where to save inside the workspace (e.g. "report.pdf").
                   If empty, uses the original filename from Drive.

    Returns:
        Success message with saved path and size, or an error string.
    """
    import io
    from googleapiclient.http import MediaIoBaseDownload
    from app.tools.filesystem import _safe_resolve, OutsideWorkspaceError
    import app.config as app_config

    try:
        service = _get_drive_service()
        meta = service.files().get(fileId=file_id, fields="mimeType,name,size").execute()
        mime_type: str = meta.get("mimeType", "")
        original_name: str = meta.get("name", "file")

        # Google Workspace native types → must be exported (can't get_media)
        _EXPORT_MAP = {
            "application/vnd.google-apps.document": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"
            ),
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"
            ),
            "application/vnd.google-apps.presentation": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"
            ),
        }

        # Determine destination path
        if save_path.strip():
            try:
                dest = _safe_resolve(save_path.strip())
            except OutsideWorkspaceError as e:
                return str(e)
        else:
            dest = app_config.WORKSPACE_DIR / original_name

        buf = io.BytesIO()

        if mime_type in _EXPORT_MAP:
            export_mime, ext = _EXPORT_MAP[mime_type]
            # Make sure the extension matches the export format
            if dest.suffix.lower() != ext:
                dest = dest.with_name(dest.stem + ext)
            content = service.files().export(fileId=file_id, mimeType=export_mime).execute()
            buf.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        elif mime_type.startswith("application/vnd.google-apps."):
            return (
                f"Error: '{original_name}' is a Google Workspace type ({mime_type}) "
                "that cannot be downloaded directly. Use drive_read to read its text content instead."
            )
        else:
            request = service.files().get_media(fileId=file_id)
            downloader = MediaIoBaseDownload(buf, request, chunksize=4 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()

        dest.parent.mkdir(parents=True, exist_ok=True)
        data = buf.getvalue()
        if not data:
            return f"Error: Downloaded 0 bytes for '{original_name}'. The file may be empty or not downloadable."
        dest.write_bytes(data)
        size_kb = len(data) / 1024
        return f"Downloaded '{original_name}' → '{dest}' ({size_kb:.1f} KB)"

    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Drive download error: {exc}"


@tool
def drive_upload(file_path: str, folder_id: str = "root", name: str = "") -> str:
    """Upload an existing file from the workspace to Google Drive.

    This action requires user approval before uploading.

    Use this when you have an actual file saved in the workspace that you want
    to put on Drive. Do NOT fabricate file content — the file must already exist.

    Works with all file types: PDFs, images, Office docs, plain text, etc.

    Args:
        file_path: Path to the existing file inside the workspace (e.g. "report.pdf").
                   The file MUST already exist — do not invent content for it.
        folder_id: Drive folder ID to upload into (default "root" = My Drive root).
        name:      File name on Drive. Defaults to the local filename if empty.

    Returns:
        JSON with {id, name, webViewLink, size_kb} on success, or an error string.
    """
    import io
    import mimetypes
    from googleapiclient.http import MediaIoBaseUpload
    from app.tools.filesystem import _safe_resolve, OutsideWorkspaceError

    try:
        src = _safe_resolve(file_path)
    except OutsideWorkspaceError as e:
        return str(e)

    if not src.exists():
        return (
            f"Error: File '{file_path}' does not exist in the workspace. "
            "Write the file first with write_file, then call drive_upload."
        )
    if not src.is_file():
        return f"Error: '{file_path}' is a directory, not a file."

    # Reject placeholder content (text files only — binary files decode to noise)
    try:
        head = src.read_bytes()[:512].decode("utf-8", errors="replace").strip().lower()
    except Exception:
        head = ""
    if any(m in head for m in _DRIVE_UPLOAD_PLACEHOLDER_MARKERS):
        return (
            f"Refusing to upload '{file_path}' — file content looks like a placeholder. "
            "Write real content with write_file first, then retry drive_upload."
        )

    drive_name = name.strip() or src.name
    mime_type, _ = mimetypes.guess_type(str(src))
    if not mime_type:
        mime_type = "application/octet-stream"

    try:
        service = _get_drive_service()

        file_bytes = src.read_bytes()
        if not file_bytes:
            return f"Error: '{file_path}' is empty. Nothing to upload."

        # Check if a file with this name already exists in the folder
        safe_name = drive_name.replace("'", "\\'")
        resp = service.files().list(
            q=f"'{folder_id}' in parents and name = '{safe_name}' and trashed = false",
            fields="files(id)",
        ).execute()
        existing = resp.get("files", [])

        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes),
            mimetype=mime_type,
            resumable=len(file_bytes) > 5 * 1024 * 1024,
        )

        if existing:
            file_id = existing[0]["id"]
            result = service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id,name,webViewLink",
            ).execute()
        else:
            result = service.files().create(
                body={"name": drive_name, "parents": [folder_id]},
                media_body=media,
                fields="id,name,webViewLink",
            ).execute()

        size_kb = len(file_bytes) / 1024
        return json.dumps({**result, "size_kb": round(size_kb, 1)}, ensure_ascii=False)

    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Drive upload error: {exc}"


# ---------------------------------------------------------------------------
# Calendar tools
# ---------------------------------------------------------------------------

@tool
def calendar_list_events(time_min: str = "", time_max: str = "", max_results: int = 10) -> str:
    """List upcoming Google Calendar events.

    Args:
        time_min: Start of time range in ISO 8601 format (e.g. "2026-04-11T00:00:00Z").
                  Defaults to right now if empty.
        time_max: End of time range in ISO 8601 format. Defaults to 7 days from now.
        max_results: Maximum number of events (default 10, max 50).

    Returns:
        JSON list of {id, summary, start, end, description, location}.
    """
    from datetime import timedelta

    max_results = min(int(max_results), 50)
    try:
        service = _get_calendar_service()

        now = datetime.now(timezone.utc)
        t_min = time_min.strip() or now.isoformat()
        t_max = time_max.strip() or (now + timedelta(days=7)).isoformat()

        resp = service.events().list(
            calendarId="primary",
            timeMin=t_min,
            timeMax=t_max,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = []
        for ev in resp.get("items", []):
            start = ev.get("start", {})
            end = ev.get("end", {})
            events.append({
                "id": ev.get("id", ""),
                "summary": ev.get("summary", "(no title)"),
                "start": start.get("dateTime") or start.get("date", ""),
                "end": end.get("dateTime") or end.get("date", ""),
                "description": ev.get("description", "")[:500],
                "location": ev.get("location", ""),
            })

        return json.dumps(events, ensure_ascii=False)
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Calendar list error: {exc}"


@tool
def calendar_create_event(
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    add_meet_link: bool = False,
) -> str:
    """Create a new event on Google Calendar.

    Set add_meet_link=True to automatically generate a Google Meet video call link for the event.

    This action requires user approval before creating.

    Args:
        summary: Event title.
        start: Start time in ISO 8601 format (e.g. "2026-04-12T14:00:00+05:30").
        end: End time in ISO 8601 format.
        description: Optional event description.
        location: Optional location string.

    Returns:
        JSON with {id, htmlLink} on success, or an error string.
    """
    try:
        service = _get_calendar_service()

        # Determine if dates include time or are all-day
        def _as_event_time(dt_str: str) -> dict:
            if "T" in dt_str:
                return {"dateTime": dt_str}
            return {"date": dt_str}

        body: dict[str, Any] = {
            "summary": summary,
            "start": _as_event_time(start),
            "end": _as_event_time(end),
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if add_meet_link:
            body["conferenceData"] = {
                "createRequest": {"requestId": summary[:60], "conferenceSolutionKey": {"type": "hangoutsMeet"}}
            }

        created = service.events().insert(
            calendarId="primary",
            body=body,
            conferenceDataVersion=1 if add_meet_link else 0,
        ).execute()
        result: dict = {"id": created.get("id", ""), "htmlLink": created.get("htmlLink", "")}
        entry_points = created.get("conferenceData", {}).get("entryPoints", [])
        meet = next((e for e in entry_points if e.get("entryPointType") == "video"), None)
        if meet:
            result["meetLink"] = meet.get("uri", "")
        return json.dumps(
            result,
            ensure_ascii=False,
        )
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Calendar create error: {exc}"


# ---------------------------------------------------------------------------
# Public list of all Google tools (for easy import in supervisor)
# ---------------------------------------------------------------------------

GOOGLE_TOOLS = [
    gmail_list_unread,
    gmail_read,
    gmail_search,
    gmail_send,
    drive_list,
    drive_read,
    drive_write,
    drive_download,
    drive_upload,
    calendar_list_events,
    calendar_create_event,
]
