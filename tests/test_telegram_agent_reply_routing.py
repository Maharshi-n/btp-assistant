"""Regression: a Telegram reply into an AGENT thread must resume via the direct
thread path, never via _run_continuation (which needs an AutomationConversation
that agent threads never have). See bug: '/switch <agent thread>' -> approve ->
'Error: conversation not found.'
"""
from app.db.models import Agent, Thread
from app.web.routes.telegram import _resolve_reply_route


async def test_agent_thread_forces_direct_route_even_with_conversation_id(db):
    agent = Agent(name="mail pinger", role_description="r", role_block="b", memory_path="agents/x.md")
    db.add(agent)
    await db.flush()
    thread = Thread(title="[Agent] mail pinger", model="gpt-4o", agent_id=agent.id)
    db.add(thread)
    await db.flush()

    # Even if a (stale/wrong) conversation_id leaked into the pending reply,
    # an agent thread must be routed direct.
    route, conv_id = await _resolve_reply_route(db, thread_id=thread.id, conversation_id=999)
    assert route == "direct"
    assert conv_id is None


async def test_non_agent_thread_with_conversation_id_uses_continuation(db):
    thread = Thread(title="Email reply", model="gpt-4o", agent_id=None)
    db.add(thread)
    await db.flush()
    route, conv_id = await _resolve_reply_route(db, thread_id=thread.id, conversation_id=42)
    assert route == "continuation"
    assert conv_id == 42


async def test_non_agent_thread_without_conversation_id_uses_direct(db):
    thread = Thread(title="Chat", model="gpt-4o", agent_id=None)
    db.add(thread)
    await db.flush()
    route, conv_id = await _resolve_reply_route(db, thread_id=thread.id, conversation_id=None)
    assert route == "direct"
    assert conv_id is None


async def test_missing_thread_with_conversation_id_uses_continuation(db):
    # Defensive: if the thread row is gone but a conversation_id exists, keep the
    # old behavior (continuation will surface its own clean error if truly gone).
    route, conv_id = await _resolve_reply_route(db, thread_id=123456, conversation_id=7)
    assert route == "continuation"
    assert conv_id == 7
