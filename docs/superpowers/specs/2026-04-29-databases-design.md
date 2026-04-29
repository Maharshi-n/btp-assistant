# RAION Databases Section — Design Spec

## Goal

Add a `/databases` section to RAION that lets users connect to external databases (SQL Server, MySQL, PostgreSQL, SQLite), auto-generates a skill file per connection so the agent can query them in natural language, and returns results as formatted text (single facts) or Excel file attachments (tabular data).

## Architecture

Three new components, following the existing Connectors pattern:

- `app/db_connections/manager.py` — DBManager singleton: connect, scan schema, execute queries, generate skill files
- `app/web/routes/databases.py` — FastAPI routes: CRUD, test connection, scan now
- `app/web/templates/databases.html` — UI: list connections, add/remove, test, scan, edit description

Credentials stored encrypted in SQLite using existing Fernet key (same as MCP connector env vars). Skill files auto-generated at `workspace/skills/db_<name>.md`.

---

## Data Model

New table `db_connections` added to `app/db/models.py`:

```python
class DBConnection(Base):
    __tablename__ = "db_connections"

    id              = Column(Integer, primary_key=True)
    name            = Column(String, unique=True, nullable=False)  # e.g. "fees_db"
    db_type         = Column(String, nullable=False)               # mssql | mysql | postgres | sqlite
    host            = Column(String, nullable=True)                # None for sqlite
    port            = Column(Integer, nullable=True)
    db_name         = Column(String, nullable=False)
    username_enc    = Column(String, nullable=True)                # Fernet encrypted
    password_enc    = Column(String, nullable=True)                # Fernet encrypted
    whitelisted_tables = Column(JSON, default=list)               # [] = all tables
    skill_description  = Column(Text, default="")                 # user-editable, injected into skill
    last_scanned_at = Column(DateTime, nullable=True)
    is_scanning     = Column(Boolean, default=False)              # scan lock flag
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
```

---

## DBManager (`app/db_connections/manager.py`)

Singleton, initialized on app startup.

### Methods

**`connect(conn: DBConnection) → engine`**
Creates SQLAlchemy engine from decrypted credentials. Supports:
- `mssql+pyodbc` — SQL Server (via ODBC Driver 17)
- `mysql+aiomysql` — MySQL
- `postgresql+asyncpg` — PostgreSQL
- `sqlite+aiosqlite` — SQLite

**`test_connection(conn: DBConnection) → bool, str`**
Attempts connection, returns (success, error_message).

**`scan_schema(conn_id: int) → None`**
- Checks `is_scanning` flag — if True, returns immediately (no concurrent scans)
- Checks `last_scanned_at` — if < 1 hour ago, returns immediately (cooldown)
- Sets `is_scanning = True`
- Reads all tables (or whitelisted tables only)
- For each table: reads column names, types, fetches 5 sample rows
- Sends batched schema (all tables in one call) to GPT-4o-mini → gets descriptions
- Regenerates skill file at `workspace/skills/db_<name>.md`
- Sets `is_scanning = False`, updates `last_scanned_at`
- On app startup: resets any `is_scanning = True` records (crash recovery)

**`execute_query(conn_id: int, sql: str) → rows, columns, row_count`**
- Enforces SELECT-only at code level — rejects any query not starting with SELECT
- Executes query, returns results
- On SQL error: triggers `scan_schema()` for that connection, raises error with message for agent retry

**`generate_result(rows, columns) → str | Path`**
- 0 rows → returns "No results found."
- Single value (1 row, 1 column) → returns formatted text string
- Multiple rows → generates Excel file in workspace/tmp/, returns file path

### Scan Lock & Cooldown

```python
async def scan_schema(self, conn_id: int):
    conn = await get_connection(conn_id)
    
    # Scan lock — no concurrent scans
    if conn.is_scanning:
        return
    
    # Cooldown — minimum 1 hour between scans
    if conn.last_scanned_at:
        elapsed = datetime.utcnow() - conn.last_scanned_at
        if elapsed.total_seconds() < 3600:
            return
    
    conn.is_scanning = True
    await db.commit()
    
    try:
        # ... perform scan ...
    finally:
        conn.is_scanning = False
        conn.last_scanned_at = datetime.utcnow()
        await db.commit()
```

### Weekly Scan (APScheduler)

- Runs **daily at 2am** (not weekly — to avoid drift)
- For each active connection, checks: `last_scanned_at < 7 days ago?`
- If yes → call `scan_schema()` (which enforces its own lock + cooldown)
- If no → skip silently

This ensures weekly cadence without drift from manual scans.

---

## Skill File Format

Auto-generated at `workspace/skills/db_<name>.md`:

```markdown
# Database: <name>
Type: SQL Server | Host: 192.168.1.x | DB: school_db

## Description
<user-editable description from UI>

## Available Tables
### students
- id (int) — primary key, unique student identifier
- name (varchar) — student full name
- roll_no (varchar) — unique roll number
- class (varchar) — class or year
- section (varchar) — section A/B/C

### fees
- student_id (int) — foreign key to students.id
- amount_total (decimal) — total fees amount due
- amount_paid (decimal) — amount paid so far
- due_date (date) — payment deadline
- status (varchar) — paid | partial | pending

## How to Query
- Tool: `query_database`
- connection_id: `<name>`
- Write standard SQL SELECT queries only
- No INSERT, UPDATE, DELETE, DROP permitted
- For ambiguous columns, refer to table descriptions above
```

Regenerated on every scan. User description block preserved across regenerations.

---

## Query Execution Flow

```
User asks DB-related question in chat / WhatsApp / Telegram
        ↓
Agent loads relevant db_<name> skill file
        ↓
Agent generates SQL SELECT query from schema in skill file
        ↓
Calls query_database tool (connection_id, sql)
        ↓
DBManager enforces SELECT-only, executes query
        ↓
On SQL error → auto re-scan → agent retries (max 3 attempts)
        ↓
Result shape check:
  - Single fact → formatted text response
  - Multiple rows → Excel file generated → sent as attachment
        ↓
Agent verifies result makes sense for query asked
  - 0 rows on broad query → agent notes this explicitly, suggests why
  - Result returned to user
```

### Result Format Decision

| Result | Format |
|--------|--------|
| Single number/fact | Text: "47 students have fees pending" |
| Multiple rows (any count) | Excel file attachment |
| 0 rows | Text: "No results found for [query]. [possible reason]" |

---

## WhatsApp / Telegram Integration

No extra work needed — agent already operates over WhatsApp and Telegram in RAION. The `query_database` tool works the same in all channels.

File sending:
- **Telegram**: existing `telegram_send` already supports files
- **WhatsApp**: `GreenAPIClient` needs one new method `send_file(chat_id, file_path)` using Green API's `sendFileByUpload` endpoint

---

## API Routes (`app/web/routes/databases.py`)

```
GET    /databases                    → HTML page
GET    /api/databases                → list all connections + status
POST   /api/databases                → create new connection
PUT    /api/databases/{id}           → update connection
DELETE /api/databases/{id}           → delete connection + remove skill file
POST   /api/databases/{id}/test      → test connection (no save)
POST   /api/databases/{id}/scan      → trigger manual scan (respects lock + cooldown)
PUT    /api/databases/{id}/description → update user description → regenerate skill file
```

---

## UI (`/databases`)

Follows connectors.html card layout exactly.

Each connection card shows:
- Name, DB type badge, host
- Status indicator: green (connected) / red (unreachable) / yellow (scanning)
- Last scanned timestamp
- **Test Connection** button
- **Scan Now** button — grays out with countdown if cooldown active ("next scan in 45m")
- **Edit Description** — inline textarea, saves on blur
- **Delete** button

Add DB modal fields:
- Name (slug, lowercase)
- Type (dropdown: SQL Server / MySQL / PostgreSQL / SQLite)
- Host, Port, Database name
- Username, Password
- Whitelisted tables (comma-separated, optional — blank = all tables)

---

## Agent Tool (`query_database`)

New tool added to agent's tool registry:

```python
@tool
async def query_database(connection_id: str, sql: str) -> str:
    """
    Execute a SELECT query against a configured database connection.
    Returns formatted text for single facts, or saves an Excel file
    and returns the file path for tabular results.
    Only SELECT queries are permitted.
    """
```

Tool added to supervisor's tool list and system prompt (alongside web, filesystem, etc.).

---

## Error Handling

| Error | Behaviour |
|-------|-----------|
| DB offline / unreachable | Clear message: "Cannot reach [name] database. Server may be offline." |
| Non-SELECT query attempted | Rejected before execution: "Only SELECT queries are permitted." |
| SQL syntax error | Auto re-scan schema → retry. After 3 failures → "Query failed, schema may have changed. Try rephrasing." |
| Scan already running | Silent skip — no duplicate scan |
| Scan cooldown active | UI shows countdown. API returns 429 with time remaining. |
| App restart mid-scan | `is_scanning` reset to False on startup for all connections |

---

## Files Created / Modified

| File | Action |
|------|--------|
| `app/db/models.py` | Add `DBConnection` model |
| `app/db_connections/__init__.py` | New package |
| `app/db_connections/manager.py` | New — DBManager singleton |
| `app/web/routes/databases.py` | New — FastAPI routes |
| `app/web/templates/databases.html` | New — UI page |
| `app/web/templates/base.html` | Add Databases sidebar link |
| `app/main.py` | Register databases router, init DBManager on startup, reset stuck scans |
| `app/tools/database.py` | New — query_database agent tool |
| `app/agents/supervisor.py` | Register query_database tool |
| `app/integrations/green_api.py` | Add send_file method |
| `requirements.txt` | Add pyodbc, aiomysql, asyncpg, openpyxl |
