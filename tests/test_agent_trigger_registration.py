from app.automations import runtime


def test_agent_cron_job_id_format():
    assert runtime._agent_job_id("cron", 5) == "agent_cron_5"
    assert runtime._agent_job_id("gmail", 9) == "agent_gmail_9"
    assert runtime._agent_job_id("selfsched", 9) == "agent_selfsched_9"


def test_register_self_scheduled_adds_date_job(monkeypatch):
    import json
    from datetime import datetime, timezone, timedelta
    added = {}

    class FakeScheduler:
        def get_job(self, jid):
            return None
        def add_job(self, func, **kw):
            added["id"] = kw.get("id")
            added["args"] = kw.get("args")

    monkeypatch.setattr(runtime, "_scheduler", FakeScheduler())

    class T:
        id = 12
        agent_id = 4
        trigger_type = "self_scheduled"
        trigger_config_json = json.dumps({
            "fire_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "note": "do the follow-up", "chain_depth": 1,
        })

    runtime.register_agent_trigger(T(), loop=None)
    assert added["id"] == "agent_selfsched_12"
    assert 4 in added["args"]


async def test_register_agent_cron_adds_job(monkeypatch):
    added = {}

    class FakeScheduler:
        def get_job(self, jid):
            return None
        def add_job(self, func, **kw):
            added["id"] = kw.get("id")
            added["args"] = kw.get("args")

    monkeypatch.setattr(runtime, "_scheduler", FakeScheduler())

    class T:
        id = 5
        agent_id = 3
        trigger_type = "cron"
        trigger_config_json = '{"cron": "0 9 * * *"}'

    runtime.register_agent_trigger(T(), loop=None)
    assert added["id"] == "agent_cron_5"
    assert 3 in added["args"]
