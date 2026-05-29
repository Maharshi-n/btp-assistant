import json
from datetime import datetime, timezone, timedelta

from app.automations import runtime as rt
from app.db.models import AgentTrigger


class _FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def get_job(self, jid):
        return self.jobs.get(jid)

    def add_job(self, func, trigger=None, id=None, **kw):
        self.jobs[id] = {"func": func, "trigger": trigger, "kw": kw}

    def remove_job(self, jid):
        self.jobs.pop(jid, None)


def test_self_scheduled_registers_date_job(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(rt, "_scheduler", fake)
    trig = AgentTrigger(
        id=77, agent_id=3, trigger_type="self_scheduled",
        trigger_config_json=json.dumps({
            "fire_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "note": "retry", "chain_depth": 1,
        }),
        created_by="self",
    )
    rt.register_agent_trigger(trig, loop=None)
    jid = rt._agent_job_id("selfsched", 77)
    assert jid in fake.jobs


def test_self_scheduled_bad_fire_at_skips(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(rt, "_scheduler", fake)
    trig = AgentTrigger(
        id=78, agent_id=3, trigger_type="self_scheduled",
        trigger_config_json=json.dumps({"fire_at": "not-a-date", "note": "x", "chain_depth": 1}),
        created_by="self",
    )
    rt.register_agent_trigger(trig, loop=None)
    assert rt._agent_job_id("selfsched", 78) not in fake.jobs
