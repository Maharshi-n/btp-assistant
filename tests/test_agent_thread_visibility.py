from sqlalchemy import select

from app.db.models import Thread


async def test_thread_has_agent_id_default_none(db):
    t = Thread(title="normal chat", model="gpt-4o-mini")
    db.add(t)
    await db.flush()
    assert t.agent_id is None


async def test_agent_thread_can_set_agent_id(db):
    t = Thread(title="[Agent] X", model="gpt-4o-mini", agent_id=5)
    db.add(t)
    await db.flush()
    assert t.agent_id == 5


async def test_main_list_query_excludes_agent_threads(db):
    db.add(Thread(title="normal", model="m"))
    db.add(Thread(title="[Agent] X", model="m", agent_id=5))
    await db.flush()
    # Mirror the list_threads main-list query: agent_id IS NULL only
    rows = (await db.execute(
        select(Thread).where(Thread.agent_id.is_(None)).order_by(Thread.created_at.desc())
    )).scalars().all()
    titles = [r.title for r in rows]
    assert "normal" in titles
    assert "[Agent] X" not in titles
