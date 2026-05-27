from app.agents import supervisor
from app.db.models import Agent, Thread


async def test_overlay_empty_when_no_agent(db, monkeypatch):
    # The default (no-agent) chat path must get an empty overlay, so normal
    # chats' system prompt is byte-for-byte unchanged.
    out = await supervisor._load_agent_overlay(None)
    assert out == ""


async def test_overlay_unknown_agent_is_empty(db, monkeypatch):
    monkeypatch.setattr(supervisor, "_load_agent_overlay", supervisor._load_agent_overlay)
    out = await supervisor._load_agent_overlay(999999)
    assert out == ""


async def test_overlay_includes_persona(monkeypatch):
    # Patch the AsyncSessionLocal used inside the overlay loader to return our agent,
    # and the memory loader to a known string.
    class _FakeAgent:
        role_block = "You are Fee Watcher."
        memory_path = "agents/x.md"

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_session():
        class _DB:
            async def get(self, model, _id):
                return _FakeAgent()
        yield _DB()

    monkeypatch.setattr("app.db.engine.AsyncSessionLocal", fake_session)
    monkeypatch.setattr("app.agents.agent_memory.load_memory_file", lambda p: "## Durable facts\n- MNDC total 142")

    out = await supervisor._load_agent_overlay(5)
    assert "AGENT ROLE" in out
    assert "You are Fee Watcher." in out
    assert "AGENT MEMORY" in out
    assert "MNDC total 142" in out


async def test_thread_agent_id_column(db):
    t = Thread(title="agent chat", model="gpt-4o-mini", agent_id=7)
    db.add(t)
    await db.flush()
    assert t.agent_id == 7
    t2 = Thread(title="normal", model="gpt-4o-mini")
    db.add(t2)
    await db.flush()
    assert t2.agent_id is None
