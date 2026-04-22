"""MCPManager — manages persistent MCP client sessions and tool discovery.

One session per enabled MCP server. stdio servers get a persistent subprocess;
SSE servers get a persistent HTTP connection. Tools are prefixed
mcp__<server_name>__<tool_name> to avoid collisions with built-in tools.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
from typing import Any

logger = logging.getLogger(__name__)


def _coerce_to_schema_dict(node: Any) -> dict:
    """Coerce a value into a valid JSON Schema node (dict).

    OpenAI rejects any non-object/non-boolean value where a schema is expected.
    Some MCP servers produce tuple-style items like:
        "items": [{"type": "number"}, {"type": "number"}]
    or even put a list at the root of `parameters`. We flatten these to a
    single object schema — picking the first element if it's a dict, or
    using anyOf for a union of primitive types.
    """
    if isinstance(node, dict):
        return node
    if isinstance(node, list):
        # Tuple-typed array → merge into anyOf, or fall back to first dict element
        dict_items = [x for x in node if isinstance(x, dict)]
        if len(dict_items) == 1:
            return dict_items[0]
        if dict_items:
            return {"anyOf": dict_items}
        return {"type": "object"}
    # bool is valid per JSON Schema spec, but OpenAI only tolerates object/bool
    if isinstance(node, bool):
        return {} if node else {"not": {}}
    # anything else (None, str, int) → permissive fallback
    return {"type": "object"}


def _sanitize_schema_node(node: Any) -> Any:
    """Recursively fix JSON Schema nodes that OpenAI rejects.

    Known bad patterns from some MCP servers:
    - "type": [{"type": "number"}, {"type": "number"}]  — tuple of objects instead of a type string
    - "type": ["string", "null"]                         — array of type strings (use anyOf instead)
    - "items": [{"type": "number"}, {"type": "number"}] — tuple-style items (draft-04) rejected by OpenAI
    - properties.X being a list or non-dict value
    - missing "type" on object nodes with "properties"
    """
    if not isinstance(node, dict):
        return node

    # Fix "type" that is a list of type-strings → convert to anyOf
    if isinstance(node.get("type"), list):
        types = node.pop("type")
        # If it's a list of dicts (objects), just pick the first valid one
        if types and isinstance(types[0], dict):
            node.update(types[0])
        else:
            # e.g. ["string", "null"] → anyOf: [{type: string}, {type: null}]
            node["anyOf"] = [{"type": t} for t in types if isinstance(t, str)]

    # Ensure object nodes with properties have type: object
    if "properties" in node and "type" not in node and "anyOf" not in node:
        node["type"] = "object"

    # Coerce + recurse into properties (each property VALUE must be a schema dict)
    props = node.get("properties")
    if isinstance(props, dict):
        for k, v in list(props.items()):
            props[k] = _sanitize_schema_node(_coerce_to_schema_dict(v))
    elif props is not None:
        # "properties" itself is not a dict → drop it
        node.pop("properties", None)

    # Coerce + recurse into anyOf / oneOf / allOf
    for key in ("anyOf", "oneOf", "allOf"):
        if key in node:
            val = node[key]
            if not isinstance(val, list):
                val = [val]
            node[key] = [_sanitize_schema_node(_coerce_to_schema_dict(s)) for s in val]

    # Coerce + recurse into items (OpenAI rejects list-valued items)
    if "items" in node:
        items = node["items"]
        if isinstance(items, list):
            # Tuple-style items → collapse to single-schema items (anyOf of members)
            coerced = [_sanitize_schema_node(_coerce_to_schema_dict(s)) for s in items]
            if len(coerced) == 1:
                node["items"] = coerced[0]
            elif coerced:
                node["items"] = {"anyOf": coerced}
            else:
                node["items"] = {}
        else:
            node["items"] = _sanitize_schema_node(_coerce_to_schema_dict(items))

    # Recurse into additionalProperties if it's a schema
    if "additionalProperties" in node and isinstance(node["additionalProperties"], (dict, list)):
        node["additionalProperties"] = _sanitize_schema_node(
            _coerce_to_schema_dict(node["additionalProperties"])
        )

    # Recurse into definitions / $defs
    for defs_key in ("definitions", "$defs"):
        defs = node.get(defs_key)
        if isinstance(defs, dict):
            for k, v in list(defs.items()):
                defs[k] = _sanitize_schema_node(_coerce_to_schema_dict(v))

    return node


def _coerce_root_object_schema(schema: Any) -> dict:
    """Ensure the root schema is a valid JSON-Schema object node.

    Some MCP servers return malformed root schemas like
    `[{"type": "number"}, {"type": "number"}]` (a list instead of an object).
    OpenAI's function-calling API rejects these with:
        Invalid schema for function '...': [...] is not of type 'object', 'boolean'.
    We coerce any such garbage into a permissive `{"type": "object"}` schema so
    the tool can still be called.
    """
    if isinstance(schema, dict):
        return schema
    # List-at-root or any other non-object → fallback to empty object schema
    return {"type": "object", "properties": {}}


def _sanitize_tool_schema(tool) -> None:
    """Fix the args_schema of a LangChain tool so OpenAI accepts it.

    langchain_mcp_adapters sets args_schema = tool.inputSchema which is a raw
    dict from the MCP server. We mutate it in-place before it reaches OpenAI.
    """
    try:
        schema = tool.args_schema
        if isinstance(schema, dict):
            before = json.dumps(schema, default=str)
            _sanitize_schema_node(schema)
            # Root must be an object schema for OpenAI function-calling.
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            after = json.dumps(schema, default=str)
            if before != after:
                logger.info("MCP: sanitized schema for %s", getattr(tool, "name", "?"))
        elif isinstance(schema, list):
            # Root schema is a list — replace it with an empty object schema.
            tool.args_schema = {"type": "object", "properties": {}}
            logger.warning(
                "MCP: tool %s had list-at-root schema; replaced with empty object schema",
                getattr(tool, "name", "?"),
            )
        elif hasattr(schema, "schema_json") or hasattr(schema, "model_json_schema"):
            # Pydantic v1 / v2 — can't easily mutate, but these are usually fine
            pass
        elif schema is None:
            tool.args_schema = {"type": "object", "properties": {}}
    except Exception as exc:
        logger.warning("MCP: could not sanitize schema for %s: %s", getattr(tool, "name", "?"), exc)
        try:
            tool.args_schema = {"type": "object", "properties": {}}
        except Exception:
            pass


class MCPManager:
    """Singleton that owns all MCP client sessions."""

    def __init__(self) -> None:
        # server_id -> {"session": ClientSession, "tools": list[BaseTool], "ctx": AsyncExitStack}
        self._sessions: dict[int, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self, server_row) -> list[str]:
        """Connect to an MCP server and discover its tools.

        Returns list of tool names on success, raises on failure.
        """
        async with self._lock:
            await self._disconnect_locked(server_row.id)
            return await self._connect_locked(server_row)

    async def disconnect(self, server_id: int) -> None:
        async with self._lock:
            await self._disconnect_locked(server_id)

    async def get_active_tools(self) -> list[BaseTool]:
        """Return all tools from all connected sessions."""
        tools: list[BaseTool] = []
        for entry in self._sessions.values():
            tools.extend(entry.get("tools", []))
        return tools

    async def test_connection(self, transport: str, command: str | None,
                              url: str | None, env: dict) -> list[str]:
        """Transient connect, list tools, disconnect. Returns tool names."""
        tools = await self._load_tools(transport, command, url, env, server_name="test")
        return [t.name for t in tools]

    async def shutdown_all(self) -> None:
        async with self._lock:
            for server_id in list(self._sessions.keys()):
                await self._disconnect_locked(server_id)

    async def reconnect_all(self, server_rows) -> None:
        """Called on startup — connect all enabled servers concurrently."""
        tasks = [self._safe_connect(row) for row in server_rows]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _safe_connect(self, server_row) -> None:
        try:
            await self.connect(server_row)
            logger.info("MCP: connected to '%s'", server_row.name)
        except Exception as exc:
            logger.warning("MCP: failed to connect to '%s': %s", server_row.name, exc)

    async def _connect_locked(self, server_row) -> list[str]:
        from contextlib import AsyncExitStack
        import app.config as _cfg
        env: dict = {}
        if server_row.env_encrypted:
            raw = server_row.env_encrypted
            decrypted_ok = False
            if _cfg.FERNET_KEY:
                try:
                    from app.mcp.crypto import decrypt_env
                    env = decrypt_env(raw)
                    decrypted_ok = True
                except Exception as exc:
                    # FERNET_KEY is set but value won't decrypt — refuse rather than
                    # silently trust whatever plaintext happens to be in the column.
                    logger.error(
                        "MCP: env_encrypted for server '%s' failed to decrypt with current FERNET_KEY: %s. "
                        "Refusing to connect with potentially tampered env.",
                        server_row.name, exc,
                    )
                    raise RuntimeError(
                        f"Encrypted env for connector '{server_row.name}' could not be decrypted. "
                        "Either the FERNET_KEY has changed or the row has been tampered with. "
                        "Re-enter the connector's env vars to re-encrypt."
                    )
            if not decrypted_ok:
                # No FERNET_KEY configured at all — accept plain JSON but warn loudly.
                try:
                    import json
                    env = json.loads(raw)
                    logger.warning(
                        "MCP: connector '%s' has plaintext env stored (FERNET_KEY not set). "
                        "Set FERNET_KEY in .env and re-save the connector to encrypt at rest.",
                        server_row.name,
                    )
                except Exception:
                    logger.warning("MCP: could not parse env for '%s' as JSON either; ignoring.", server_row.name)

        stack = AsyncExitStack()
        try:
            tools = await self._load_tools(
                transport=server_row.transport,
                command=server_row.command,
                url=server_row.url,
                env=env,
                server_name=server_row.name,
                stack=stack,
            )
        except Exception:
            await stack.aclose()
            raise

        self._sessions[server_row.id] = {
            "tools": tools,
            "stack": stack,
            "name": server_row.name,
        }
        return [t.name for t in tools]

    async def _disconnect_locked(self, server_id: int) -> None:
        entry = self._sessions.pop(server_id, None)
        if entry:
            try:
                await entry["stack"].aclose()
            except Exception as exc:
                logger.debug("MCP: error closing session %s: %s", server_id, exc)

    async def _load_tools(
        self,
        transport: str,
        command: str | None,
        url: str | None,
        env: dict,
        server_name: str,
        stack=None,
    ) -> list[BaseTool]:
        """Create a session (optionally owned by stack), list tools, return wrapped list."""
        from contextlib import AsyncExitStack
        from langchain_mcp_adapters.tools import load_mcp_tools

        own_stack = stack is None
        if own_stack:
            stack = AsyncExitStack()

        try:
            if transport == "stdio":
                session = await self._stdio_session(command, env, stack)
            else:
                session = await self._sse_session(url, env, stack)

            raw_tools = await load_mcp_tools(session)
        except Exception:
            if own_stack:
                await stack.aclose()
            raise

        if own_stack:
            # transient — close immediately after listing
            await stack.aclose()

        # Prefix tool names to avoid collisions — mutate name in-place
        safe = server_name.lower().replace(" ", "_").replace("-", "_")
        for t in raw_tools:
            t.name = f"mcp__{safe}__{t.name}"
            _sanitize_tool_schema(t)
        return raw_tools

    async def _stdio_session(self, command: str, env: dict, stack):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import _create_platform_compatible_process, _get_executable_command
        from mcp.shared.message import SessionMessage
        import anyio
        import anyio.lowlevel
        from anyio.streams.text import TextReceiveStream
        import mcp.types as types
        import subprocess
        import os

        if not command:
            raise ValueError("stdio transport requires a command")

        argv = shlex.split(command)
        merged_env = {**os.environ, **env}

        # We bypass stdio_client's errlog parameter entirely by spawning the
        # process ourselves with stderr=subprocess.PIPE, then manually building
        # the read/write streams. This avoids the fileno() error on Windows where
        # uvicorn wraps sys.stderr in a _Tee that has no real file descriptor.
        exe = _get_executable_command(argv[0])
        process = await _create_platform_compatible_process(
            command=exe,
            args=argv[1:],
            env=merged_env,
            errlog=subprocess.PIPE,  # real pipe, no fileno() needed
        )

        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

        async def stdout_reader():
            assert process.stdout
            try:
                async with read_stream_writer:
                    buffer = ""
                    async for chunk in TextReceiveStream(process.stdout, encoding="utf-8"):
                        lines = (buffer + chunk).split("\n")
                        buffer = lines.pop()
                        for line in lines:
                            try:
                                message = types.JSONRPCMessage.model_validate_json(line)
                                await read_stream_writer.send(SessionMessage(message))
                            except Exception as exc:
                                await read_stream_writer.send(exc)
            except anyio.ClosedResourceError:
                await anyio.lowlevel.checkpoint()

        async def stdin_writer():
            assert process.stdin
            try:
                async with write_stream_reader:
                    async for session_message in write_stream_reader:
                        json_str = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                        await process.stdin.send((json_str + "\n").encode("utf-8"))
            except anyio.ClosedResourceError:
                await anyio.lowlevel.checkpoint()

        tg_ctx = anyio.create_task_group()
        tg = await stack.enter_async_context(tg_ctx)
        await stack.enter_async_context(process)
        tg.start_soon(stdout_reader)
        tg.start_soon(stdin_writer)

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session

    async def _sse_session(self, url: str, env: dict, stack):
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        if not url:
            raise ValueError("sse transport requires a url")

        # env vars for SSE are typically passed as headers or query params —
        # pass as extra_headers if an Authorization token is present
        headers = {}
        if env.get("AUTHORIZATION"):
            headers["Authorization"] = env["AUTHORIZATION"]
        elif env.get("TOKEN"):
            headers["Authorization"] = f"Bearer {env['TOKEN']}"

        read, write = await stack.enter_async_context(sse_client(url, headers=headers))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session


# Global singleton
_manager: MCPManager | None = None


def get_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager
