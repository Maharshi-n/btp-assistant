"""DBManager — connect, scan schema, execute queries for external databases."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import app.config as app_config
from app.db.engine import AsyncSessionLocal
from app.db.models import DBConnection

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _fernet():
    from cryptography.fernet import Fernet
    key = app_config.FERNET_KEY
    if not key:
        raise RuntimeError("FERNET_KEY not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(token: str) -> str:
    if not token:
        return ""
    return _fernet().decrypt(token.encode()).decode()


def normalize_name(raw: str) -> str:
    name = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not _NAME_RE.match(name):
        raise ValueError(
            "DB connection name must be 1-64 chars, lowercase letters, digits, or underscore."
        )
    return name


def encrypt_credentials(username: str | None, password: str | None) -> tuple[str | None, str | None]:
    u = _encrypt(username) if username else None
    p = _encrypt(password) if password else None
    return u, p


def decrypt_credentials(conn: DBConnection) -> tuple[str, str]:
    username = _decrypt(conn.username_enc) if conn.username_enc else ""
    password = _decrypt(conn.password_enc) if conn.password_enc else ""
    return username, password


def build_url(conn: DBConnection) -> str:
    from urllib.parse import quote
    username, password = decrypt_credentials(conn)
    u = quote(username, safe="") if username else ""
    p = quote(password, safe="") if password else ""
    if conn.db_type == "sqlite":
        return f"sqlite+aiosqlite:///{conn.db_name}"
    if conn.db_type == "mssql":
        driver = "ODBC+Driver+17+for+SQL+Server"
        return (
            f"mssql+aioodbc://{u}:{p}@{conn.host}:{conn.port or 1433}"
            f"/{conn.db_name}?driver={driver}"
        )
    if conn.db_type == "mysql":
        return f"mysql+aiomysql://{u}:{p}@{conn.host}:{conn.port or 3306}/{conn.db_name}"
    if conn.db_type == "postgres":
        return f"postgresql+asyncpg://{u}:{p}@{conn.host}:{conn.port or 5432}/{conn.db_name}"
    raise ValueError(f"Unknown db_type: {conn.db_type!r}")


async def test_connection(conn: DBConnection) -> tuple[bool, str]:
    """Try connecting; return (success, error_message)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    url = build_url(conn)
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as c:
            from sqlalchemy import text
            await c.execute(text("SELECT 1"))
        return True, ""
    except Exception as exc:
        return False, str(exc)[:500]
    finally:
        await engine.dispose()


async def _get_tables(conn: DBConnection) -> list[str]:
    """Return list of table names to scan (all or whitelisted)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    url = build_url(conn)
    engine = create_async_engine(url)
    try:
        whitelisted = json.loads(conn.whitelisted_tables or "[]")
        async with engine.connect() as c:
            if conn.db_type == "sqlite":
                result = await c.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                )
                all_tables = [row[0] for row in result]
            elif conn.db_type == "mssql":
                result = await c.execute(
                    text("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'")
                )
                all_tables = [row[0] for row in result]
            elif conn.db_type == "mysql":
                result = await c.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
                )
                all_tables = [row[0] for row in result]
            elif conn.db_type == "postgres":
                result = await c.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
                )
                all_tables = [row[0] for row in result]
            else:
                all_tables = []
        return [t for t in all_tables if not whitelisted or t in whitelisted]
    finally:
        await engine.dispose()


async def _get_foreign_keys(conn: DBConnection) -> dict[str, list[dict]]:
    """Return declared foreign keys per table: {table: [{column, ref_table, ref_column}, ...]}.

    These are REAL constraints the database declares — not guesses. If the database
    declares no foreign keys (common in legacy ERPs), this returns an empty dict.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    url = build_url(conn)
    engine = create_async_engine(url)
    fks: dict[str, list[dict]] = {}
    try:
        async with engine.connect() as c:
            if conn.db_type == "sqlite":
                # Per-table PRAGMA — need the table list first
                tbl_result = await c.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                )
                tables = [row[0] for row in tbl_result]
                for table in tables:
                    if not re.match(r'^[A-Za-z0-9_$#]+$', table):
                        continue
                    fk_result = await c.execute(text(f"PRAGMA foreign_key_list({table})"))
                    for row in fk_result:
                        # PRAGMA foreign_key_list cols: id, seq, table, from, to, ...
                        fks.setdefault(table, []).append(
                            {"column": row[3], "ref_table": row[2], "ref_column": row[4]}
                        )
            elif conn.db_type in ("mssql", "mysql", "postgres"):
                # Standard ANSI INFORMATION_SCHEMA referential constraints
                query = text(
                    "SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME, "
                    "       ccu.TABLE_NAME AS REF_TABLE, ccu.COLUMN_NAME AS REF_COLUMN "
                    "FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc "
                    "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
                    "  ON rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                    "JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu "
                    "  ON rc.UNIQUE_CONSTRAINT_NAME = ccu.CONSTRAINT_NAME"
                )
                try:
                    result = await c.execute(query)
                    for row in result:
                        fks.setdefault(row[0], []).append(
                            {"column": row[1], "ref_table": row[2], "ref_column": row[3]}
                        )
                except Exception as exc:
                    # MySQL lacks CONSTRAINT_COLUMN_USAGE — fall back to its KEY_COLUMN_USAGE form
                    if conn.db_type == "mysql":
                        my_query = text(
                            "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                            "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL"
                        )
                        result = await c.execute(my_query)
                        for row in result:
                            fks.setdefault(row[0], []).append(
                                {"column": row[1], "ref_table": row[2], "ref_column": row[3]}
                            )
                    else:
                        logger.warning("FK extraction failed for %s: %s", conn.name, exc)
        return fks
    except Exception as exc:
        logger.warning("Foreign key scan failed for %s: %s", conn.name, exc)
        return {}
    finally:
        await engine.dispose()


async def _get_columns_and_samples(conn: DBConnection, table: str) -> tuple[list[dict], list[list]]:
    """Return (columns_info, sample_rows) for a single table."""
    if not re.match(r'^[A-Za-z0-9_$#]+$', table):
        raise ValueError(f"Invalid table name: {table!r}")
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    url = build_url(conn)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as c:
            if conn.db_type == "sqlite":
                result = await c.execute(text(f"PRAGMA table_info({table})"))
                columns = [{"name": row[1], "type": row[2]} for row in result]
            elif conn.db_type in ("mssql", "mysql", "postgres"):
                result = await c.execute(
                    text(
                        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                        f"WHERE TABLE_NAME = :t"
                    ),
                    {"t": table},
                )
                columns = [{"name": row[0], "type": row[1]} for row in result]
            else:
                columns = []

            if conn.db_type == "mssql":
                samples_result = await c.execute(text(f"SELECT TOP 5 * FROM {table}"))
            else:
                samples_result = await c.execute(text(f"SELECT * FROM {table} LIMIT 5"))
            def _serialize(v):
                if v is None:
                    return None
                if isinstance(v, (int, float, bool, str)):
                    return v
                return str(v)
            samples = [[_serialize(v) for v in row] for row in samples_result.fetchall()]
        return columns, samples
    finally:
        await engine.dispose()


async def _describe_schema_with_llm(conn_name: str, tables_data: dict) -> dict[str, dict[str, str]]:
    """Send all table schemas to GPT-4o-mini; get column descriptions back."""
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)

    schema_text = f"Database: {conn_name}\n\n"
    for table, info in tables_data.items():
        schema_text += f"Table: {table}\nColumns: {json.dumps(info['columns'])}\nSample rows (up to 5): {json.dumps(info['samples'])}\n\n"

    prompt = (
        "You are analyzing a database schema. For each column in each table, "
        "write a concise one-line description (what it stores, any notable constraints). "
        "Return a JSON object with this structure:\n"
        '{"table_name": {"column_name": "description", ...}, ...}\n\n'
        "Schema:\n" + schema_text
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        logger.warning("LLM schema description failed: %s", exc)
        return {}


# Markers around the hand-written section that scans MUST preserve.
_NOTES_BEGIN = "<!-- MANUAL_NOTES_BEGIN -->"
_NOTES_END = "<!-- MANUAL_NOTES_END -->"
_NOTES_PLACEHOLDER = (
    "_Write join rules, business logic, and query recipes here. "
    "Anything between the BEGIN/END markers is PRESERVED across re-scans._\n\n"
    "Examples:\n"
    "- To find a student's pending fees: join `fees` to `students` on `student_id`, "
    "filter `paid = 0`.\n"
    "- \"Active student\" means `status = 'A'`.\n"
    "- The yearly result table is built by joining `exam_results` to `exams` on `exam_id`."
)


def _extract_manual_notes(existing_content: str | None) -> str:
    """Pull the hand-written notes block (between markers) out of the old file.

    Returns the inner text if found, else a starter placeholder so the user
    knows where to write.
    """
    if existing_content:
        start = existing_content.find(_NOTES_BEGIN)
        end = existing_content.find(_NOTES_END)
        if start != -1 and end != -1 and end > start:
            inner = existing_content[start + len(_NOTES_BEGIN):end].strip("\n")
            if inner.strip():
                return inner
    return _NOTES_PLACEHOLDER


def _generate_skill_content(
    conn: DBConnection,
    tables_data: dict,
    descriptions: dict[str, dict[str, str]],
    foreign_keys: dict[str, list[dict]],
    manual_notes: str,
) -> str:
    """Build the skill file markdown content."""
    type_label = {"mssql": "SQL Server", "mysql": "MySQL", "postgres": "PostgreSQL", "sqlite": "SQLite"}.get(conn.db_type, conn.db_type)
    host_part = f"Host: {conn.host} | " if conn.host else ""
    lines = [
        f"# Database: {conn.name}",
        f"Type: {type_label} | {host_part}DB: {conn.db_name}",
        "",
        "## Description",
        conn.skill_description or "(no description set)",
        "",
        "## Available Tables",
    ]
    for table, info in tables_data.items():
        cols = ", ".join(f"{col['name']} ({col['type']})" for col in info["columns"])
        lines.append(f"- **{table}**: {cols}")
        # Show up to 2 real sample rows so the agent sees actual data shape/values.
        col_names = [col["name"] for col in info["columns"]]
        for sample in info.get("samples", [])[:2]:
            pairs = ", ".join(
                f"{name}={_fmt_sample(val)}"
                for name, val in zip(col_names, sample)
            )
            lines.append(f"    - e.g. {pairs}")
    lines.append("")

    # Declared foreign-key relationships — facts from the DB, not guesses.
    fk_lines: list[str] = []
    for table, rels in foreign_keys.items():
        for rel in rels:
            fk_lines.append(
                f"- `{table}.{rel['column']}` → `{rel['ref_table']}.{rel['ref_column']}`"
            )
    lines += ["## Relationships (declared foreign keys)"]
    if fk_lines:
        lines += fk_lines
    else:
        lines.append(
            "_No foreign keys are declared in this database. Tables may still be "
            "linked by matching column names/values — see the manual notes below._"
        )
    lines.append("")

    # Hand-written notes — PRESERVED across re-scans.
    lines += [
        "## Notes & Relationships (hand-written — preserved across scans)",
        _NOTES_BEGIN,
        manual_notes,
        _NOTES_END,
        "",
    ]

    lines += [
        "## How to Query",
        "- Tool: `query_database`",
        f"- connection_id: `{conn.name}`",
        "- Write standard SQL SELECT queries only",
        "- No INSERT, UPDATE, DELETE, DROP permitted",
        "- For ambiguous columns, refer to the sample rows and table descriptions above",
        "- For joins, prefer the declared foreign keys; otherwise consult the manual notes",
    ]
    return "\n".join(lines)


def _fmt_sample(val: Any) -> str:
    """Format a sample cell value compactly for the skill file."""
    if val is None:
        return "NULL"
    s = str(val)
    if len(s) > 40:
        s = s[:37] + "..."
    return s


async def _generate_trigger_description(conn: DBConnection, skill_content: str) -> str:
    """Ask LLM to generate a good skill trigger description based on the schema."""
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)
    prompt = (
        "You are configuring an AI assistant. Based on the database skill file below, "
        "write a single concise trigger description (max 30 words) that tells the AI when to use this skill. "
        "Focus on the kinds of questions a user would ask — use natural language topics, not database/table names. "
        "Example: 'use when the user asks about students, fees, attendance, exam results, admissions, or staff at the college'\n\n"
        f"Skill file:\n{skill_content[:2000]}"
    )
    try:
        response = await client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        trigger = response.choices[0].message.content.strip().strip('"').strip("'")
        return trigger or f"use when the user asks about the {conn.name} database"
    except Exception as exc:
        logger.warning("Failed to generate trigger description: %s", exc)
        return f"use when the user asks about the {conn.name} database (tables, records, queries)"


async def _write_skill_file(conn: DBConnection, content: str) -> None:
    """Write skill file and upsert DB row."""
    from sqlalchemy import select
    from app.db.models import Skill

    skills_dir = (app_config.WORKSPACE_DIR / "skills").resolve()
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_name = f"db_{conn.name}"
    file_path = (skills_dir / f"{skill_name}.md").resolve()
    try:
        file_path.relative_to(skills_dir)
    except ValueError:
        raise ValueError(f"Refusing to write skill file outside skills dir: {file_path}")
    file_path.write_text(content, encoding="utf-8")
    relative_path = f"skills/{skill_name}.md"

    trigger = await _generate_trigger_description(conn, content)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Skill).where(Skill.name == skill_name))
        existing = result.scalars().first()
        if existing:
            existing.trigger_description = trigger
            existing.file_path = relative_path
            existing.enabled = True
        else:
            db.add(Skill(name=skill_name, trigger_description=trigger, file_path=relative_path, enabled=True))
        await db.commit()

    try:
        from app.agents.supervisor import invalidate_skills_cache
        invalidate_skills_cache()
    except Exception:
        pass


async def scan_schema(conn_id: int, force: bool = False) -> None:
    """Scan schema for a connection, update skill file.

    force=True bypasses the 1h cooldown (used for manual user-triggered scans).
    """
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DBConnection).where(DBConnection.id == conn_id))
        conn = result.scalar_one_or_none()
        if not conn:
            return

        if conn.is_scanning:
            return

        if not force and conn.last_scanned_at:
            elapsed = (datetime.now(timezone.utc) - conn.last_scanned_at.replace(tzinfo=timezone.utc)).total_seconds()
            if elapsed < 3600:
                return

        conn.is_scanning = True
        await db.commit()
        conn_snapshot = DBConnection(
            id=conn.id, name=conn.name, db_type=conn.db_type,
            host=conn.host, port=conn.port, db_name=conn.db_name,
            username_enc=conn.username_enc, password_enc=conn.password_enc,
            whitelisted_tables=conn.whitelisted_tables,
            skill_description=conn.skill_description,
        )

    try:
        tables = await _get_tables(conn_snapshot)
        tables_data: dict[str, dict] = {}
        for table in tables:
            columns, samples = await _get_columns_and_samples(conn_snapshot, table)
            tables_data[table] = {"columns": columns, "samples": samples}

        foreign_keys = await _get_foreign_keys(conn_snapshot)

        # Preserve any hand-written notes from the previous skill file.
        skills_dir = (app_config.WORKSPACE_DIR / "skills").resolve()
        prev_path = skills_dir / f"db_{conn_snapshot.name}.md"
        prev_content = prev_path.read_text(encoding="utf-8") if prev_path.exists() else None
        manual_notes = _extract_manual_notes(prev_content)

        descriptions = await _describe_schema_with_llm(conn_snapshot.name, tables_data)
        skill_content = _generate_skill_content(
            conn_snapshot, tables_data, descriptions, foreign_keys, manual_notes
        )
        await _write_skill_file(conn_snapshot, skill_content)
        logger.info("Schema scan complete for %s (%d tables)", conn_snapshot.name, len(tables))
    except Exception as exc:
        logger.error("Schema scan failed for %s: %s", conn_snapshot.name, exc)
    finally:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(DBConnection).where(DBConnection.id == conn_id))
            conn = result.scalar_one_or_none()
            if conn:
                conn.is_scanning = False
                conn.last_scanned_at = datetime.now(timezone.utc)
                await db.commit()


async def execute_query(conn_id: int, sql: str) -> tuple[list[list], list[str], int]:
    """Execute a SELECT query. Returns (rows, columns, row_count).

    Raises ValueError for non-SELECT queries.
    Triggers scan_schema on SQL error and re-raises.
    """
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are permitted.")

    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DBConnection).where(DBConnection.id == conn_id))
        conn = result.scalar_one_or_none()
    if not conn:
        raise ValueError(f"Database connection ID {conn_id} not found.")

    url = build_url(conn)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as c:
            result = await c.execute(text(sql))
            columns = list(result.keys())
            rows = [list(row) for row in result.fetchall()]
            return rows, columns, len(rows)
    except Exception as exc:
        logger.warning("SQL error on %s — triggering re-scan: %s", conn.name, exc)
        import asyncio
        asyncio.create_task(scan_schema(conn_id))
        raise
    finally:
        await engine.dispose()


def generate_result(rows: list[list], columns: list[str]) -> str | Path:
    """Convert query results to text (single fact) or Excel file path (tabular)."""
    if not rows:
        return "No results found."
    if len(rows) == 1 and len(columns) == 1:
        return str(rows[0][0])

    import openpyxl
    tmp_dir = app_config.WORKSPACE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    import time
    file_path = tmp_dir / f"query_result_{int(time.time())}.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(columns)
    for row in rows:
        ws.append([str(v) if v is not None else "" for v in row])
    wb.save(file_path)
    return file_path


async def reset_stuck_scans() -> None:
    """On startup: reset is_scanning=True for any connections left mid-scan."""
    from sqlalchemy import update
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(DBConnection).where(DBConnection.is_scanning == True).values(is_scanning=False)  # noqa: E712
        )
        await db.commit()


async def register_weekly_scan_job() -> None:
    """Register APScheduler job that runs daily at 2am and scans connections older than 7 days."""
    try:
        from app.automations.runtime import _scheduler
        from apscheduler.triggers.cron import CronTrigger
        from sqlalchemy import select

        if _scheduler is None:
            return

        async def _weekly_scan_job():
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(DBConnection).where(DBConnection.is_active == True))  # noqa: E712
                conns = result.scalars().all()
            for conn in conns:
                if conn.last_scanned_at is None:
                    await scan_schema(conn.id)
                else:
                    age = (datetime.now(timezone.utc) - conn.last_scanned_at.replace(tzinfo=timezone.utc)).total_seconds()
                    if age > 7 * 24 * 3600:
                        await scan_schema(conn.id)

        _scheduler.add_job(
            _weekly_scan_job,
            CronTrigger(hour=2, minute=0),
            id="db_weekly_scan",
            replace_existing=True,
        )
        logger.info("Registered DB scan job (runs daily at 2am, scans connections older than 7 days)")
    except Exception as exc:
        logger.warning("Failed to register weekly DB scan job: %s", exc)
