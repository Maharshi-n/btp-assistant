import json
from datetime import datetime, timezone, timedelta

import pytest

from app.agents import agent_schedule as sched
from app.db.models import Agent, AgentTrigger


async def _agent(db):
    a = Agent(name="x", role_description="r", role_block="b", memory_path="agents/x.md")
    db.add(a)
    await db.flush()
    return a


def test_parse_when_relative_hours():
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    out = sched.parse_when("in 2 hours", now=now)
    assert out == now + timedelta(hours=2)


def test_parse_when_relative_minutes():
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    assert sched.parse_when("in 30 minutes", now=now) == now + timedelta(minutes=30)


def test_parse_when_iso():
    out = sched.parse_when("2026-06-01T09:00:00+00:00")
    assert out == datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def test_parse_when_invalid_returns_none():
    assert sched.parse_when("sometime later maybe") is None


async def test_schedule_self_creates_trigger(db, monkeypatch):
    a = await _agent(db)
    registered = []
    monkeypatch.setattr(sched, "register_agent_trigger", lambda t, loop=None: registered.append(t))
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    msg = await sched.schedule_self(db, a.id, when_iso=when.isoformat(), note="retry the fetch", chain_depth=0)
    assert "scheduled" in msg.lower()
    trigs = (await db.execute(AgentTrigger.__table__.select())).fetchall()
    assert len(trigs) == 1
    cfg = json.loads(trigs[0].trigger_config_json)
    assert cfg["note"] == "retry the fetch"
    assert cfg["chain_depth"] == 1  # stored depth = incoming + 1
    assert trigs[0].created_by == "self"
    assert len(registered) == 1


async def test_schedule_self_respects_depth_cap(db, monkeypatch):
    a = await _agent(db)
    monkeypatch.setattr(sched, "register_agent_trigger", lambda t, loop=None: None)
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    msg = await sched.schedule_self(db, a.id, when_iso=when.isoformat(), note="loop",
                                    chain_depth=sched.SELF_WAKE_MAX_DEPTH)
    assert "limit" in msg.lower()
    trigs = (await db.execute(AgentTrigger.__table__.select())).fetchall()
    assert len(trigs) == 0  # refused


async def test_schedule_self_dedups_same_note(db, monkeypatch):
    a = await _agent(db)
    monkeypatch.setattr(sched, "register_agent_trigger", lambda t, loop=None: None)
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    await sched.schedule_self(db, a.id, when_iso=when.isoformat(), note="same", chain_depth=0)
    msg = await sched.schedule_self(db, a.id, when_iso=when.isoformat(), note="same", chain_depth=0)
    assert "already" in msg.lower()
    trigs = (await db.execute(AgentTrigger.__table__.select())).fetchall()
    assert len(trigs) == 1  # second was a no-op


async def test_create_trigger_validates(db, monkeypatch):
    a = await _agent(db)
    monkeypatch.setattr(sched, "register_agent_trigger", lambda t, loop=None: None)
    msg = await sched.create_trigger(db, a.id, "cron", {"cron": "0 9 * * *"}, "daily 9am")
    assert "created" in msg.lower()
    bad = await sched.create_trigger(db, a.id, "cron", {"cron": "nope"}, "x")
    assert "invalid" in bad.lower() or "error" in bad.lower()


async def test_list_and_delete_own_triggers(db, monkeypatch):
    a = await _agent(db)
    other = await _agent(db)
    monkeypatch.setattr(sched, "register_agent_trigger", lambda t, loop=None: None)
    monkeypatch.setattr(sched, "unregister_agent_trigger", lambda tid: None)
    await sched.create_trigger(db, a.id, "cron", {"cron": "0 9 * * *"}, "mine")
    await sched.create_trigger(db, other.id, "cron", {"cron": "0 8 * * *"}, "theirs")
    listing = await sched.list_triggers(db, a.id)
    assert "mine" not in listing  # listing shows configs, not descriptions
    # but it must only show this agent's triggers — assert by counting
    assert listing.count("#") == 1

    mine_id = (await db.execute(
        AgentTrigger.__table__.select().where(AgentTrigger.agent_id == a.id)
    )).fetchall()[0].id
    ok = await sched.delete_trigger(db, a.id, mine_id)
    assert "deleted" in ok.lower()

    theirs_id = (await db.execute(
        AgentTrigger.__table__.select().where(AgentTrigger.agent_id == other.id)
    )).fetchall()[0].id
    refused = await sched.delete_trigger(db, a.id, theirs_id)
    assert "not" in refused.lower()
