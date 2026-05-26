from datetime import datetime, timezone

from app.db.models import Agent, AgentRun, AgentTrigger


async def test_agent_roundtrip(db):
    agent = Agent(
        name="Fee Watcher",
        role_description="Watch overdue fees.",
        role_block="You are Fee Watcher.",
        memory_path="agents/1_memory.md",
    )
    db.add(agent)
    await db.flush()
    assert agent.id is not None
    assert agent.model == "gpt-5.4-mini"
    assert agent.memory_model == "gpt-4o-mini"
    assert agent.status == "active"
    assert agent.memory_watermark == 0
    assert agent.daily_fire_budget == 100
    assert agent.fires_today == 0


async def test_agent_trigger_and_run(db):
    agent = Agent(name="A", role_description="r", role_block="b", memory_path="agents/x.md")
    db.add(agent)
    await db.flush()

    trig = AgentTrigger(
        agent_id=agent.id,
        trigger_type="cron",
        trigger_config_json='{"cron": "0 9 * * *"}',
    )
    run = AgentRun(
        agent_id=agent.id,
        started_at=datetime.now(timezone.utc),
        status="running",
        thread_id=42,
        trigger_summary="woke on cron",
    )
    db.add(trig)
    db.add(run)
    await db.flush()
    assert trig.id is not None and trig.enabled is True
    assert run.id is not None and run.status == "running"
