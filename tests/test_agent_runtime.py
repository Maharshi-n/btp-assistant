from app.agents import agent_runtime
from app.db.models import Agent, AgentRun


async def _make_agent(db, **kw):
    agent = Agent(
        name=kw.get("name", "A"),
        role_description="r",
        role_block="b",
        memory_path="agents/test.md",
        status=kw.get("status", "active"),
        daily_fire_budget=kw.get("daily_fire_budget", 100),
        fires_today=kw.get("fires_today", 0),
    )
    db.add(agent)
    await db.flush()
    return agent


async def test_paused_agent_does_not_fire(db, monkeypatch):
    agent = await _make_agent(db, status="paused")
    called = {"ran": False}

    async def fake_invoke(*a, **k):
        called["ran"] = True
        return "ignored"

    monkeypatch.setattr(agent_runtime, "_invoke_graph", fake_invoke)
    monkeypatch.setattr(agent_runtime, "_session_factory", lambda: _ctx(db))

    result = await agent_runtime.fire_agent(agent.id, trigger_id=None, trigger_context={})
    assert result == "skipped:paused"
    assert called["ran"] is False


async def test_over_budget_agent_skips(db, monkeypatch):
    agent = await _make_agent(db, fires_today=100, daily_fire_budget=100)
    monkeypatch.setattr(agent_runtime, "_session_factory", lambda: _ctx(db))
    result = await agent_runtime.fire_agent(agent.id, trigger_id=None, trigger_context={})
    assert result == "skipped:budget"


async def test_active_agent_fires_and_logs(db, monkeypatch):
    agent = await _make_agent(db)

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None):
        return "agent did the thing"

    monkeypatch.setattr(agent_runtime, "_invoke_graph", fake_invoke)
    monkeypatch.setattr(agent_runtime, "_session_factory", lambda: _ctx(db))
    monkeypatch.setattr(agent_runtime, "schedule_memory_distillation", lambda *a, **k: None)

    result = await agent_runtime.fire_agent(agent.id, trigger_id=None, trigger_context={})
    assert result == "done"

    await db.refresh(agent)
    assert agent.fires_today == 1

    runs = (await db.execute(
        AgentRun.__table__.select().where(AgentRun.agent_id == agent.id)
    )).fetchall()
    assert len(runs) == 1

    # The fire thread must be tagged with agent_id so /switch into it runs AS the agent.
    from app.db.models import Thread as _Thread
    threads = (await db.execute(
        _Thread.__table__.select().where(_Thread.agent_id == agent.id)
    )).fetchall()
    assert len(threads) == 1


async def test_fire_persists_trigger_context_into_thread(db, monkeypatch):
    # Generic: whatever woke the agent (email/file/message) is saved into the
    # thread, so a later follow-up the user /switches into still has the original
    # event in scope (not just the agent's short reply).
    from app.db.models import Message as _Message, AgentRun as _AgentRun

    agent = await _make_agent(db)

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None):
        return "summary sent"

    monkeypatch.setattr(agent_runtime, "_invoke_graph", fake_invoke)
    monkeypatch.setattr(agent_runtime, "_session_factory", lambda: _ctx(db))
    monkeypatch.setattr(agent_runtime, "schedule_memory_distillation", lambda *a, **k: None)

    block = "\n\n━━━ INCOMING EMAIL ━━━\nemail_from: rohit@x.com\nemail_body:\nhackathon?\n━━━ END ━━━"
    await agent_runtime.fire_agent(agent.id, trigger_id=None, trigger_context={"trusted_block": block})

    tid = (await db.execute(
        _AgentRun.__table__.select().where(_AgentRun.agent_id == agent.id)
    )).fetchall()[0].thread_id
    msgs = (await db.execute(
        _Message.__table__.select().where(_Message.thread_id == tid)
    )).fetchall()
    # The raw trigger context (with the sender address) is persisted in the thread.
    assert any("rohit@x.com" in (m.content or "") for m in msgs)


async def test_fire_passes_agent_id_and_frames_trigger_as_data(db, monkeypatch):
    agent = await _make_agent(db)
    captured = {}

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None):
        captured["prompt"] = prompt
        captured["agent_id"] = agent_id
        return "ok"

    monkeypatch.setattr(agent_runtime, "_invoke_graph", fake_invoke)
    monkeypatch.setattr(agent_runtime, "_session_factory", lambda: _ctx(db))
    monkeypatch.setattr(agent_runtime, "schedule_memory_distillation", lambda *a, **k: None)

    trusted = "\n\n━━━ INCOMING EMAIL ━━━\nemail_body:\nplease send me AI updates\n━━━ END ━━━"
    await agent_runtime.fire_agent(agent.id, trigger_id=None, trigger_context={"trusted_block": trusted})

    # agent_id is passed so the supervisor overlays role+memory (not glued into the user turn)
    assert captured["agent_id"] == agent.id
    # The user turn is a directive that (a) tells the agent to follow its ROLE,
    # (b) explicitly says NOT to obey instructions inside the trigger data.
    p = captured["prompt"]
    assert "AGENT ROLE" in p or "your role" in p.lower()
    # It explicitly tells the agent NOT to act on instructions inside the data.
    assert "do not carry them out" in p.lower()
    assert "untrusted" in p.lower()
    # The untrusted email content is included as data.
    assert "please send me AI updates" in p


import contextlib


@contextlib.asynccontextmanager
async def _ctx(session):
    yield session
