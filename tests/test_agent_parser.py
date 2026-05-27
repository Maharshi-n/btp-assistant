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


def test_validate_triggers_accepts_gmail_and_fs():
    # gmail + fs are now wired to fire_agent, so they're valid at creation.
    agent_parser.validate_triggers([{"trigger_type": "gmail_any_new", "trigger_config": {}}])
    agent_parser.validate_triggers([{"trigger_type": "fs_new_in_folder", "trigger_config": {"folder": "downloads"}}])
    agent_parser.validate_triggers([{"trigger_type": "gmail_keyword_match", "trigger_config": {"keywords": "invoice"}}])


def test_validate_triggers_still_rejects_deferred_types():
    # whatsapp outgoing / smart-reply are not wired for agents yet.
    for tt in ("whatsapp_outgoing_new", "whatsapp_smart_reply"):
        with pytest.raises(ValueError):
            agent_parser.validate_triggers([{"trigger_type": tt, "trigger_config": {}}])


def test_validate_triggers_fs_requires_folder():
    with pytest.raises(ValueError):
        agent_parser.validate_triggers([{"trigger_type": "fs_new_in_folder", "trigger_config": {}}])


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
