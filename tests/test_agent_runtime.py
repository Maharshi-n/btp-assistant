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

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id):
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


async def test_agent_reuses_rolling_thread_across_fires(db, monkeypatch):
    from app.db.models import Thread
    agent = await _make_agent(db)

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id):
        return "ok"

    monkeypatch.setattr(agent_runtime, "_invoke_graph", fake_invoke)
    monkeypatch.setattr(agent_runtime, "_session_factory", lambda: _ctx(db))
    monkeypatch.setattr(agent_runtime, "schedule_memory_distillation", lambda *a, **k: None)

    await agent_runtime.fire_agent(agent.id, trigger_id=None, trigger_context={})
    await agent_runtime.fire_agent(agent.id, trigger_id=None, trigger_context={})

    # Two fires, but only ONE thread for this agent (rolling reuse), and both
    # runs point at that same thread.
    threads = (await db.execute(
        Thread.__table__.select().where(Thread.agent_id == agent.id)
    )).fetchall()
    assert len(threads) == 1
    runs = (await db.execute(
        AgentRun.__table__.select().where(AgentRun.agent_id == agent.id)
    )).fetchall()
    assert len(runs) == 2
    assert {r.thread_id for r in runs} == {threads[0].id}


import contextlib


@contextlib.asynccontextmanager
async def _ctx(session):
    yield session
