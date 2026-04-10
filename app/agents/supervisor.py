"""Phase 4: minimal LangGraph supervisor (one 'chat' node, no tools).

The graph has a single node called 'chat' that calls OpenAI via ChatOpenAI.
It uses AsyncSqliteSaver for checkpointing so state survives server restarts.

Callers pass the model name via the LangGraph invocation config:

    config = {
        "configurable": {
            "thread_id": str(thread_id),
            "model": "gpt-4o",
        }
    }
"""
from __future__ import annotations

import aiosqlite
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph

import app.config as app_config


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def _build_graph(checkpointer: AsyncSqliteSaver) -> object:
    """Compile the supervisor StateGraph with the given checkpointer."""

    async def chat_node(state: MessagesState, config: RunnableConfig) -> dict:
        """Single node: forward full message history to OpenAI; return reply."""
        model_name: str = config.get("configurable", {}).get("model", "gpt-4o")
        llm = ChatOpenAI(
            model=model_name,
            api_key=app_config.OPENAI_API_KEY,
            streaming=True,
        )
        response: BaseMessage = await llm.ainvoke(state["messages"])
        return {"messages": [response]}

    builder: StateGraph = StateGraph(MessagesState)
    builder.add_node("chat", chat_node)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# DB path helper
# ---------------------------------------------------------------------------


def _resolve_db_path() -> str:
    """Return the filesystem path to the SQLite DB file from DATABASE_URL."""
    url = app_config.DATABASE_URL
    # url looks like "sqlite+aiosqlite:///E:\BTP project\app.db"
    if ":///" in url:
        return url.split("///", 1)[-1]
    return "app.db"


# ---------------------------------------------------------------------------
# Module-level singletons managed by init/shutdown helpers called from main.py
# ---------------------------------------------------------------------------

_conn: aiosqlite.Connection | None = None
_checkpointer: AsyncSqliteSaver | None = None
_graph: object | None = None


def get_graph() -> object:
    """Return the compiled graph; raises if init_supervisor() was not called."""
    if _graph is None:
        raise RuntimeError("LangGraph supervisor not initialised — did on_startup run?")
    return _graph


async def init_supervisor() -> None:
    """Open the aiosqlite connection, wire up AsyncSqliteSaver, compile the graph.

    Called once from app startup (before the first request).
    """
    global _conn, _checkpointer, _graph
    db_path = _resolve_db_path()
    _conn = await aiosqlite.connect(db_path)
    _checkpointer = AsyncSqliteSaver(_conn)
    # Ensure the checkpointer tables exist
    await _checkpointer.setup()
    _graph = _build_graph(_checkpointer)


async def shutdown_supervisor() -> None:
    """Close the aiosqlite connection.  Called once from app shutdown."""
    global _conn, _checkpointer, _graph
    _graph = None
    _checkpointer = None
    if _conn is not None:
        try:
            await _conn.close()
        except Exception:
            pass
        _conn = None
