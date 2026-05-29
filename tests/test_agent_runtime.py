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
        return ("ignored", [])

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

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None, chain_depth=0):
        return ("agent did the thing", [])

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

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None, chain_depth=0):
        return ("summary sent", [])

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

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None, chain_depth=0):
        captured["prompt"] = prompt
        captured["agent_id"] = agent_id
        return ("ok", [])

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


async def test_fire_persists_action_summary_when_text_empty(db, monkeypatch):
    # The agent ends an autonomous fire on a tool call (telegram_send) with no
    # trailing text — final_content is "". The thread must STILL get a visible
    # assistant message summarizing the action, so /switch isn't blank.
    from app.db.models import Message as _Message, AgentRun as _AgentRun

    agent = await _make_agent(db)

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None, chain_depth=0):
        return ("", ["telegram_send: New email from Rohit about a hackathon"])

    monkeypatch.setattr(agent_runtime, "_invoke_graph", fake_invoke)
    monkeypatch.setattr(agent_runtime, "_session_factory", lambda: _ctx(db))
    monkeypatch.setattr(agent_runtime, "schedule_memory_distillation", lambda *a, **k: None)

    await agent_runtime.fire_agent(agent.id, trigger_id=None, trigger_context={})

    tid = (await db.execute(
        _AgentRun.__table__.select().where(_AgentRun.agent_id == agent.id)
    )).fetchall()[0].thread_id
    msgs = (await db.execute(
        _Message.__table__.select()
        .where(_Message.thread_id == tid)
        .where(_Message.role == "assistant")
    )).fetchall()
    assert len(msgs) == 1
    assert "telegram_send" in (msgs[0].content or "")


def test_describe_tool_call_surfaces_message():
    out = agent_runtime._describe_tool_call(
        {"name": "telegram_send", "args": {"message": "hello world"}}
    )
    assert out == "telegram_send: hello world"
    # No useful arg → just the tool name.
    assert agent_runtime._describe_tool_call({"name": "gmail_list_unread", "args": {}}) == "gmail_list_unread"


async def test_fire_writes_episode(db, monkeypatch, tmp_path):
    from app.agents import agent_runtime as ar, agent_episodes as ep
    agent = await _make_agent(db)

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None, chain_depth=0):
        return ("did the thing", ["telegram_send: hi"])

    monkeypatch.setattr(ar, "_invoke_graph", fake_invoke)
    monkeypatch.setattr(ar, "_session_factory", lambda: _ctx(db))
    monkeypatch.setattr(ar, "schedule_memory_distillation", lambda *a, **k: None)
    epfile = tmp_path / "ep.jsonl"
    monkeypatch.setattr(ar, "_episode_path_for", lambda agent: epfile)

    await ar.fire_agent(agent.id, trigger_id=None, trigger_context={"trigger_type": "cron"})
    rows = ep.read_episodes(epfile)
    assert len(rows) == 1
    assert "did the thing" in rows[0]["summary"]
    assert rows[0]["trigger_type"] == "cron"


async def test_fire_passes_chain_depth_into_invoke(db, monkeypatch, tmp_path):
    from app.agents import agent_runtime as ar
    agent = await _make_agent(db)
    captured = {}

    async def fake_invoke(prompt, model, lg_thread_id, ws_thread_id, agent_id=None, chain_depth=0):
        captured["chain_depth"] = chain_depth
        return ("ok", [])

    monkeypatch.setattr(ar, "_invoke_graph", fake_invoke)
    monkeypatch.setattr(ar, "_session_factory", lambda: _ctx(db))
    monkeypatch.setattr(ar, "schedule_memory_distillation", lambda *a, **k: None)
    monkeypatch.setattr(ar, "_episode_path_for", lambda agent: tmp_path / "none.jsonl")

    await ar.fire_agent(agent.id, trigger_id=None, trigger_context={"chain_depth": 3})
    assert captured["chain_depth"] == 3


import contextlib


@contextlib.asynccontextmanager
async def _ctx(session):
    yield session
