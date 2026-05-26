from app.automations import runtime


def test_agent_cron_job_id_format():
    assert runtime._agent_job_id("cron", 5) == "agent_cron_5"
    assert runtime._agent_job_id("gmail", 9) == "agent_gmail_9"


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
