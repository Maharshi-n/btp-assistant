import pytest

from app.agents import agent_parser


def test_validate_triggers_accepts_known_types():
    triggers = [
        {"trigger_type": "cron", "trigger_config": {"cron": "0 9 * * *"}},
        {"trigger_type": "whatsapp_group_new", "trigger_config": {"chat_id": ""}},
    ]
    agent_parser.validate_triggers(triggers)


def test_validate_triggers_rejects_unknown_type():
    with pytest.raises(ValueError):
        agent_parser.validate_triggers([{"trigger_type": "telepathy", "trigger_config": {}}])


def test_validate_triggers_rejects_deferred_unwired_types():
    # Trigger types that aren't wired to fire_agent yet must be rejected at
    # creation so users can't make an agent that silently never fires.
    for tt in ("gmail_any_new", "whatsapp_outgoing_new", "fs_new_in_folder"):
        with pytest.raises(ValueError):
            agent_parser.validate_triggers([{"trigger_type": tt, "trigger_config": {}}])


def test_validate_triggers_rejects_bad_cron():
    with pytest.raises(ValueError):
        agent_parser.validate_triggers([{"trigger_type": "cron", "trigger_config": {"cron": "not a cron"}}])


async def test_finalize_agent_spec_parses_llm_json(monkeypatch):
    async def fake_llm(messages):
        return (
            '{"role_block": "You are Fee Watcher.", '
            '"triggers": [{"trigger_type": "cron", "trigger_config": {"cron": "0 9 * * *"}}]}'
        )

    monkeypatch.setattr(agent_parser, "_call_llm", fake_llm)
    spec = await agent_parser.finalize_agent_spec(
        role_description="Watch fees", interview_answers="9am, all colleges, telegram"
    )
    assert spec["role_block"] == "You are Fee Watcher."
    assert spec["triggers"][0]["trigger_type"] == "cron"
