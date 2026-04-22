"""TTL-cached MCP tool loader — mirrors the skills/memory cache pattern."""
from __future__ import annotations

import asyncio
import time
import logging
from typing import Any

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

_mcp_cache: dict[str, Any] = {"tools": [], "loaded_at": 0.0, "ttl": 15.0}
_refresh_lock = asyncio.Lock()


def invalidate_mcp_cache() -> None:
    _mcp_cache["loaded_at"] = 0.0


async def load_active_mcp_tools() -> list[BaseTool]:
    """Return all tools from connected MCP servers, with TTL caching.

    A single refresh lock coalesces parallel cache-miss callers so only one
    actually hits the MCP manager; the rest wait and read the fresh cache.
    """
    now = time.monotonic()
    if now - _mcp_cache["loaded_at"] < _mcp_cache["ttl"]:
        return _mcp_cache["tools"]

    async with _refresh_lock:
        # Re-check — another caller may have populated the cache while we waited.
        now = time.monotonic()
        if now - _mcp_cache["loaded_at"] < _mcp_cache["ttl"]:
            return _mcp_cache["tools"]

        from app.mcp.manager import get_manager, _sanitize_tool_schema
        try:
            tools = await get_manager().get_active_tools()
        except Exception as exc:
            logger.warning("MCP: failed to load tools: %s", exc)
            tools = []

        for t in tools:
            _sanitize_tool_schema(t)

        _mcp_cache["tools"] = tools
        _mcp_cache["loaded_at"] = now
        return tools


