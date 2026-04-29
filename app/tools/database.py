"""query_database — agent tool for executing SELECT queries against configured databases."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
async def query_database(connection_id: str, sql: str) -> str:
    """Execute a SELECT query against a configured database connection.

    Returns formatted text for single facts, or saves an Excel file
    and returns the file path for tabular results.
    Only SELECT queries are permitted.

    Args:
        connection_id: The name of the database connection (e.g. 'fees_db').
        sql: A valid SQL SELECT statement.
    """
    from sqlalchemy import select
    from app.db.engine import AsyncSessionLocal
    from app.db.models import DBConnection
    from app.db_connections.manager import execute_query, generate_result

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DBConnection).where(DBConnection.name == connection_id))
        conn = result.scalar_one_or_none()

    if not conn:
        return f"Error: No database connection named '{connection_id}' found. Check /databases to see configured connections."

    if not conn.is_active:
        return f"Error: Database connection '{connection_id}' is disabled."

    try:
        rows, columns, row_count = await execute_query(conn.id, sql)
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return (
            f"SQL error on '{connection_id}': {exc}. "
            "Schema re-scan triggered — you may retry once the scan completes."
        )

    result_val = generate_result(rows, columns)
    if isinstance(result_val, str):
        return result_val

    # Excel file — return path for agent to attach/send
    return f"Results saved to: {result_val}\nRow count: {row_count}\nColumns: {', '.join(columns)}"
