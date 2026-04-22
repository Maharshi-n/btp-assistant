"""Connectors routes — MCP server management.

Endpoints:
  GET    /connectors                          → HTML page
  GET    /api/connectors                      → list servers + tools + health
  POST   /api/connectors                      → create server
  PUT    /api/connectors/{id}                 → update server
  DELETE /api/connectors/{id}                 → delete server
  POST   /api/connectors/{id}/enable          → enable
  POST   /api/connectors/{id}/disable         → disable
  POST   /api/connectors/{id}/refresh         → re-discover tools
  POST   /api/connectors/test                 → test without saving
  POST   /api/connectors/{id}/tools/{tid}/permission → set auto/ask
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as app_config
from app.db.engine import get_db
from app.db.models import MCPServer, MCPTool, Skill, User
from app.web.deps import require_user

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ServerCreate(BaseModel):
    name: str
    transport: str          # "stdio" | "sse"
    command: str | None = None
    url: str | None = None
    env: dict = {}          # plain dict — encrypted before storage


class ServerUpdate(BaseModel):
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    url: str | None = None
    env: dict | None = None


class ToolPermission(BaseModel):
    permission: str         # "auto" | "ask"


_NAME_RE = __import__("re").compile(r"^[a-z0-9_]{1,64}$")


def _normalize_connector_name(raw: str) -> str:
    """Normalize a user-supplied connector name. Raises HTTPException on invalid."""
    name = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not _NAME_RE.match(name):
        raise HTTPException(
            422,
            "Connector name must be 1-64 chars, lowercase letters, digits, or underscore.",
        )
    return name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encrypt(env: dict) -> str | None:
    if not env:
        return None
    if not app_config.FERNET_KEY:
        # No Fernet key — store as plain JSON (warn in logs)
        logger.warning("FERNET_KEY not set — storing MCP env vars unencrypted")
        import json
        return json.dumps(env)
    from app.mcp.crypto import encrypt_env
    return encrypt_env(env)


def _server_dict(server: MCPServer, tools: list[MCPTool]) -> dict:
    return {
        "id": server.id,
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "url": server.url,
        "enabled": server.enabled,
        "status": server.status,
        "last_error": server.last_error,
        "created_at": server.created_at.isoformat(),
        "tools": [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "permission": t.permission,
                "enabled": t.enabled,
            }
            for t in tools
        ],
    }


async def _sync_tools(db: AsyncSession, server: MCPServer, tool_names: list[str], all_lc_tools) -> None:
    """Persist discovered tools to MCPTool table."""
    # Delete old tools for this server
    await db.execute(delete(MCPTool).where(MCPTool.server_id == server.id))

    lc_map = {t.name: t for t in all_lc_tools}
    for name in tool_names:
        lc = lc_map.get(name)
        schema = "{}"
        if lc and hasattr(lc, "args_schema") and lc.args_schema:
            try:
                schema = json.dumps(lc.args_schema.schema())
            except Exception:
                pass
        db.add(MCPTool(
            server_id=server.id,
            name=name,
            description=lc.description if lc else "",
            input_schema_json=schema,
            permission="ask",
            enabled=True,
        ))


def _invalidate():
    try:
        from app.mcp.loader import invalidate_mcp_cache
        invalidate_mcp_cache()
    except Exception:
        pass


async def _upsert_mcp_skill(db: AsyncSession, server: MCPServer, lc_tools: list) -> None:
    """Create or update a skill file + DB row for an MCP server."""
    skill_name = f"mcp_{server.name}"
    trigger = f"use when the user asks about {server.name.replace('_', ' ')} (MCP connector)"

    # Build skill file content
    lines = [
        f"# {server.name} MCP Connector",
        "",
        f"You have FULL access to {server.name} via MCP tools. Never say you lack access.",
        "Never ask the user for IDs — use search/list tools to find them first.",
        "",
        "## Available Tools",
    ]
    for t in lc_tools:
        lines.append(f"- `{t.name}`: {t.description}")

    # Notion-specific guidance injected automatically
    if server.name == "notion":
        lines += [
            "",
            "## Notion Rules",
            "- Always call `mcp__notion__API-post-search` first to find pages or databases.",
            "- To create a page in a database: search first to get the database ID, then call `mcp__notion__API-post-page`.",
            "- To read a page: use `mcp__notion__API-retrieve-a-page` or `mcp__notion__API-get-block-children`.",
            "- NEVER ask the user for a database ID.",
        ]

    content = "\n".join(lines)

    # Write skill file to workspace/skills/
    skills_dir = (app_config.WORKSPACE_DIR / "skills").resolve()
    skills_dir.mkdir(parents=True, exist_ok=True)
    file_path = (skills_dir / f"{skill_name}.md").resolve()
    # Defense in depth: refuse anything that escapes skills_dir
    try:
        file_path.relative_to(skills_dir)
    except ValueError:
        logger.error("Refusing to write MCP skill file outside skills dir: %s", file_path)
        raise HTTPException(400, "Invalid skill file path")
    file_path.write_text(content, encoding="utf-8")
    relative_path = f"skills/{skill_name}.md"

    # Upsert DB row
    result = await db.execute(select(Skill).where(Skill.name == skill_name))
    existing = result.scalars().first()
    if existing:
        existing.trigger_description = trigger
        existing.file_path = relative_path
        existing.enabled = True
    else:
        db.add(Skill(name=skill_name, trigger_description=trigger, file_path=relative_path, enabled=True))

    # Invalidate skills cache so agent picks it up immediately
    try:
        from app.agents.supervisor import invalidate_skills_cache
        invalidate_skills_cache()
    except Exception:
        pass


async def _delete_mcp_skill(db: AsyncSession, server_name: str) -> None:
    """Remove the skill file + DB row for an MCP server."""
    skill_name = f"mcp_{server_name}"
    result = await db.execute(select(Skill).where(Skill.name == skill_name))
    skill = result.scalars().first()
    if skill:
        # Delete the file
        try:
            file_path = app_config.WORKSPACE_DIR / skill.file_path
            if file_path.exists():
                file_path.unlink()
        except Exception:
            pass
        await db.delete(skill)
    try:
        from app.agents.supervisor import invalidate_skills_cache
        invalidate_skills_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@router.get("/connectors")
async def connectors_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(select(MCPServer).order_by(MCPServer.created_at.desc()))
    servers = result.scalars().all()

    server_data = []
    for s in servers:
        tr = await db.execute(select(MCPTool).where(MCPTool.server_id == s.id))
        tools = tr.scalars().all()
        server_data.append(_server_dict(s, tools))

    return templates.TemplateResponse(
        "connectors.html",
        {"request": request, "server_data": server_data},
    )


# ---------------------------------------------------------------------------
# API: list
# ---------------------------------------------------------------------------

@router.get("/api/connectors")
async def list_connectors(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    result = await db.execute(select(MCPServer).order_by(MCPServer.created_at.desc()))
    servers = result.scalars().all()
    out = []
    for s in servers:
        tr = await db.execute(select(MCPTool).where(MCPTool.server_id == s.id))
        tools = tr.scalars().all()
        out.append(_server_dict(s, tools))
    return out


# ---------------------------------------------------------------------------
# API: create
# ---------------------------------------------------------------------------

@router.post("/api/connectors")
async def create_connector(
    body: ServerCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    name = _normalize_connector_name(body.name)
    if body.transport not in ("stdio", "sse"):
        raise HTTPException(422, "transport must be stdio or sse")
    if body.transport == "stdio" and not body.command:
        raise HTTPException(422, "stdio transport requires a command")
    if body.transport == "sse" and not body.url:
        raise HTTPException(422, "sse transport requires a url")

    existing = await db.execute(select(MCPServer).where(MCPServer.name == name))
    if existing.scalars().first():
        raise HTTPException(409, f"Connector '{name}' already exists")

    server = MCPServer(
        name=name,
        transport=body.transport,
        command=body.command.strip() if body.command else None,
        url=body.url.strip() if body.url else None,
        env_encrypted=_encrypt(body.env),
        enabled=True,
        status="unknown",
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)

    # Connect and discover tools
    tool_names: list[str] = []
    try:
        from app.mcp.manager import get_manager
        mgr = get_manager()
        lc_tools = []
        # Use internal _load_tools to get the LangChain tool objects back
        tool_names_raw = await mgr.connect(server)
        lc_tools = await mgr.get_active_tools()
        # Filter to only tools from this server prefix
        prefix = f"mcp__{name}__"
        lc_tools = [t for t in lc_tools if t.name.startswith(prefix)]
        tool_names = [t.name for t in lc_tools]

        await _sync_tools(db, server, tool_names, lc_tools)
        await _upsert_mcp_skill(db, server, lc_tools)
        server.status = "ok"
        server.last_error = None
    except Exception as exc:
        server.status = "error"
        server.last_error = str(exc)[:500]
        logger.warning("MCP connect failed for '%s': %s", name, exc)

    await db.commit()
    await db.refresh(server)
    _invalidate()

    tr = await db.execute(select(MCPTool).where(MCPTool.server_id == server.id))
    return _server_dict(server, tr.scalars().all())


# ---------------------------------------------------------------------------
# API: update
# ---------------------------------------------------------------------------

@router.put("/api/connectors/{server_id}")
async def update_connector(
    server_id: int,
    body: ServerUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    server = await db.get(MCPServer, server_id)
    if not server:
        raise HTTPException(404, "Connector not found")

    if body.name is not None:
        server.name = _normalize_connector_name(body.name)
    if body.transport is not None:
        if body.transport not in ("stdio", "sse"):
            raise HTTPException(422, "transport must be stdio or sse")
        server.transport = body.transport
    if body.command is not None:
        server.command = body.command
    if body.url is not None:
        server.url = body.url
    if body.env is not None:
        server.env_encrypted = _encrypt(body.env)

    await db.commit()

    # Reconnect
    try:
        from app.mcp.manager import get_manager
        mgr = get_manager()
        await mgr.connect(server)
        prefix = f"mcp__{server.name}__"
        lc_tools = [t for t in await mgr.get_active_tools() if t.name.startswith(prefix)]
        await _sync_tools(db, server, [t.name for t in lc_tools], lc_tools)
        await _upsert_mcp_skill(db, server, lc_tools)
        server.status = "ok"
        server.last_error = None
    except Exception as exc:
        server.status = "error"
        server.last_error = str(exc)[:500]

    await db.commit()
    _invalidate()

    tr = await db.execute(select(MCPTool).where(MCPTool.server_id == server.id))
    return _server_dict(server, tr.scalars().all())


# ---------------------------------------------------------------------------
# API: delete
# ---------------------------------------------------------------------------

@router.delete("/api/connectors/{server_id}")
async def delete_connector(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    server = await db.get(MCPServer, server_id)
    if not server:
        raise HTTPException(404, "Connector not found")

    try:
        from app.mcp.manager import get_manager
        await get_manager().disconnect(server_id)
    except Exception:
        pass

    await _delete_mcp_skill(db, server.name)
    await db.execute(delete(MCPTool).where(MCPTool.server_id == server_id))
    await db.delete(server)
    await db.commit()
    _invalidate()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# API: enable / disable
# ---------------------------------------------------------------------------

@router.post("/api/connectors/{server_id}/enable")
async def enable_connector(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    server = await db.get(MCPServer, server_id)
    if not server:
        raise HTTPException(404, "Connector not found")
    server.enabled = True
    await db.commit()
    try:
        from app.mcp.manager import get_manager
        mgr = get_manager()
        await mgr.connect(server)
        prefix = f"mcp__{server.name}__"
        lc_tools = [t for t in await mgr.get_active_tools() if t.name.startswith(prefix)]
        await _upsert_mcp_skill(db, server, lc_tools)
        server.status = "ok"
        server.last_error = None
        await db.commit()
    except Exception as exc:
        server.status = "error"
        server.last_error = str(exc)[:500]
        await db.commit()
    _invalidate()
    return {"id": server_id, "enabled": True}


@router.post("/api/connectors/{server_id}/disable")
async def disable_connector(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    server = await db.get(MCPServer, server_id)
    if not server:
        raise HTTPException(404, "Connector not found")
    server.enabled = False
    await db.commit()
    try:
        from app.mcp.manager import get_manager
        await get_manager().disconnect(server_id)
    except Exception:
        pass
    await _delete_mcp_skill(db, server.name)
    await db.commit()
    _invalidate()
    return {"id": server_id, "enabled": False}


# ---------------------------------------------------------------------------
# API: refresh tools
# ---------------------------------------------------------------------------

@router.post("/api/connectors/{server_id}/refresh")
async def refresh_connector(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    server = await db.get(MCPServer, server_id)
    if not server:
        raise HTTPException(404, "Connector not found")

    try:
        from app.mcp.manager import get_manager
        mgr = get_manager()
        await mgr.connect(server)
        prefix = f"mcp__{server.name}__"
        lc_tools = [t for t in await mgr.get_active_tools() if t.name.startswith(prefix)]
        await _sync_tools(db, server, [t.name for t in lc_tools], lc_tools)
        await _upsert_mcp_skill(db, server, lc_tools)
        server.status = "ok"
        server.last_error = None
    except Exception as exc:
        server.status = "error"
        server.last_error = str(exc)[:500]

    await db.commit()
    _invalidate()

    tr = await db.execute(select(MCPTool).where(MCPTool.server_id == server.id))
    return _server_dict(server, tr.scalars().all())


# ---------------------------------------------------------------------------
# API: test connection
# ---------------------------------------------------------------------------

class TestRequest(BaseModel):
    transport: str
    command: str | None = None
    url: str | None = None
    env: dict = {}


@router.post("/api/connectors/test")
async def test_connector(
    body: TestRequest,
    _user: User = Depends(require_user),
):
    try:
        from app.mcp.manager import get_manager
        tool_names = await get_manager().test_connection(
            transport=body.transport,
            command=body.command,
            url=body.url,
            env=body.env,
        )
        return {"ok": True, "tools": tool_names}
    except Exception as exc:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# API: set tool permission
# ---------------------------------------------------------------------------

@router.post("/api/connectors/{server_id}/tools/{tool_id}/permission")
async def set_tool_permission(
    server_id: int,
    tool_id: int,
    body: ToolPermission,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    if body.permission not in ("auto", "ask"):
        raise HTTPException(422, "permission must be auto or ask")
    tool = await db.get(MCPTool, tool_id)
    if not tool or tool.server_id != server_id:
        raise HTTPException(404, "Tool not found")
    tool.permission = body.permission
    await db.commit()
    return {"id": tool_id, "permission": body.permission}
