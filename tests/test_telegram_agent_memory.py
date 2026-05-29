"""When the user replies inside an AGENT thread on Telegram (e.g. 'reply him with X'),
that interaction must be distilled into the agent's memory — the agent should know its
whole environment, including what the user told it to do. Plain (non-agent) chat threads
must NOT trigger agent memory distillation.
"""
from app.db.models import Agent, Thread
from app.web.routes import telegram as tg


async def test_agent_thread_reply_schedules_distillation(db, monkeypatch):
    agent = Agent(name="mail pinger", role_description="r", role_block="b", memory_path="agents/x.md")
    db.add(agent)
    await db.flush()
    thread = Thread(title="[Agent] mail pinger", model="gpt-4o", agent_id=agent.id)
    db.add(thread)
    await db.flush()

    calls = []
    monkeypatch.setattr(tg, "schedule_chat_distillation", lambda aid, tid: calls.append((aid, tid)))
    # Point the helper at our test session.
    monkeypatch.setattr(tg, "AsyncSessionLocal", lambda: _ctx(db))

    await tg._distil_agent_thread_if_agent(thread.id)
    assert calls == [(agent.id, thread.id)]


async def test_plain_thread_reply_does_not_distil(db, monkeypatch):
    thread = Thread(title="Chat", model="gpt-4o", agent_id=None)
    db.add(thread)
    await db.flush()

    calls = []
    monkeypatch.setattr(tg, "schedule_chat_distillation", lambda aid, tid: calls.append((aid, tid)))
    monkeypatch.setattr(tg, "AsyncSessionLocal", lambda: _ctx(db))

    await tg._distil_agent_thread_if_agent(thread.id)
    assert calls == []


import contextlib


@contextlib.asynccontextmanager
async def _ctx(session):
    yield session
