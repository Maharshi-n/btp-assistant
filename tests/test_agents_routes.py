from app.web.routes import agents as agents_routes
from app.db.models import Agent, AgentTrigger


async def test_create_agent_persists_agent_and_triggers(db, monkeypatch):
    async def fake_finalize(role_description, interview_answers):
        return {
            "role_block": "You are Fee Watcher.",
            "triggers": [{"trigger_type": "cron", "trigger_config": {"cron": "0 9 * * *"}}],
        }
    monkeypatch.setattr(agents_routes, "finalize_agent_spec", fake_finalize)
    monkeypatch.setattr(agents_routes, "register_agent_trigger", lambda *a, **k: None)
    monkeypatch.setattr(agents_routes, "_init_memory_file", lambda *a, **k: None)

    agent = await agents_routes.create_agent_record(
        db, name="Fee Watcher", role_description="watch fees", interview_answers="9am"
    )
    assert agent.id is not None
    assert agent.role_block == "You are Fee Watcher."

    trigs = (await db.execute(
        AgentTrigger.__table__.select().where(AgentTrigger.agent_id == agent.id)
    )).fetchall()
    assert len(trigs) == 1


async def test_new_agent_chat_creates_linked_thread(db, monkeypatch):
    from app.db.models import Thread
    agent = Agent(name="Chatty", role_description="r", role_block="b", memory_path="agents/c.md")
    db.add(agent)
    await db.flush()

    # Call the endpoint function directly with our test session.
    resp = await agents_routes.api_new_agent_chat(agent.id, db=db, _u=None)
    import json as _json
    body = _json.loads(resp.body)
    tid = body["thread_id"]

    thread = await db.get(Thread, tid)
    assert thread is not None
    assert thread.agent_id == agent.id  # thread is linked to the agent
    assert thread.model == agent.model


async def test_delete_agent_removes_agent_triggers_and_runs(db, monkeypatch):
    from datetime import datetime, timezone
    from app.db.models import AgentRun
    monkeypatch.setattr(agents_routes, "unregister_agent_trigger", lambda *a, **k: None)

    agent = Agent(name="D", role_description="r", role_block="b", memory_path="agents/none.md")
    db.add(agent)
    await db.flush()
    db.add(AgentTrigger(agent_id=agent.id, trigger_type="cron", trigger_config_json='{"cron": "0 9 * * *"}'))
    db.add(AgentRun(agent_id=agent.id, started_at=datetime.now(timezone.utc), status="done", thread_id=1))
    await db.flush()
    aid = agent.id

    await agents_routes.delete_agent_record(db, aid)

    assert (await db.get(Agent, aid)) is None
    trigs = (await db.execute(
        AgentTrigger.__table__.select().where(AgentTrigger.agent_id == aid)
    )).fetchall()
    runs = (await db.execute(
        AgentRun.__table__.select().where(AgentRun.agent_id == aid)
    )).fetchall()
    assert len(trigs) == 0
    assert len(runs) == 0


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def test_list_triggers_endpoint(db):
    agent = Agent(name="T", role_description="r", role_block="b", memory_path="agents/t.md")
    db.add(agent)
    await db.flush()
    db.add(AgentTrigger(agent_id=agent.id, trigger_type="cron",
                        trigger_config_json='{"cron": "0 9 * * *"}', created_by="user"))
    await db.flush()

    resp = await agents_routes.api_list_triggers(agent.id, db=db, _u=None)
    import json as _json
    rows = _json.loads(resp.body)
    assert len(rows) == 1
    assert rows[0]["trigger_type"] == "cron"
    assert rows[0]["created_by"] == "user"


async def test_create_and_delete_trigger_endpoint(db, monkeypatch):
    monkeypatch.setattr(agents_routes, "register_agent_trigger", lambda *a, **k: None)
    monkeypatch.setattr(agents_routes, "unregister_agent_trigger", lambda *a, **k: None)
    agent = Agent(name="T", role_description="r", role_block="b", memory_path="agents/t.md")
    db.add(agent)
    await db.flush()

    import json as _json
    req = _FakeRequest({"trigger_type": "cron", "trigger_config": {"cron": "0 9 * * *"}})
    resp = await agents_routes.api_create_trigger(agent.id, req, db=db, _u=None)
    tid = _json.loads(resp.body)["id"]

    # created_by="user" for UI-created triggers
    from app.db.models import AgentTrigger as _AT
    row = await db.get(_AT, tid)
    assert row.created_by == "user"

    resp2 = await agents_routes.api_delete_trigger(agent.id, tid, db=db, _u=None)
    assert _json.loads(resp2.body)["deleted"] == tid


async def test_create_trigger_rejects_invalid(db, monkeypatch):
    monkeypatch.setattr(agents_routes, "register_agent_trigger", lambda *a, **k: None)
    from fastapi import HTTPException
    import pytest
    agent = Agent(name="T", role_description="r", role_block="b", memory_path="agents/t.md")
    db.add(agent)
    await db.flush()
    req = _FakeRequest({"trigger_type": "cron", "trigger_config": {"cron": "bad"}})
    with pytest.raises(HTTPException):
        await agents_routes.api_create_trigger(agent.id, req, db=db, _u=None)


async def test_episodes_endpoint(db):
    agent = Agent(name="T", role_description="r", role_block="b", memory_path="agents/t.md")
    db.add(agent)
    await db.flush()
    resp = await agents_routes.api_episodes(agent.id, q="", since="", db=db, _u=None)
    import json as _json
    assert _json.loads(resp.body) == []


async def test_pause_resume_flips_status(db, monkeypatch):
    agent = Agent(name="A", role_description="r", role_block="b", memory_path="agents/a.md")
    db.add(agent)
    await db.flush()
    monkeypatch.setattr(agents_routes, "unregister_agent_trigger", lambda *a, **k: None)
    monkeypatch.setattr(agents_routes, "register_agent_trigger", lambda *a, **k: None)

    await agents_routes.set_agent_status(db, agent.id, "paused")
    await db.refresh(agent)
    assert agent.status == "paused"

    await agents_routes.set_agent_status(db, agent.id, "active")
    await db.refresh(agent)
    assert agent.status == "active"
