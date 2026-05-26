from app.agents.agent_prompt import build_agent_prompt


def test_build_agent_prompt_has_all_blocks_in_order():
    prompt = build_agent_prompt(
        base_prompt="BASE RAION PROMPT",
        role_block="You are Fee Watcher.",
        memory_text="## Durable facts\n- MNDC total: 142",
        trigger_context="chat_id: 123",
    )
    assert "BASE RAION PROMPT" in prompt
    assert "You are Fee Watcher." in prompt
    assert "MNDC total: 142" in prompt
    assert "chat_id: 123" in prompt
    assert prompt.index("BASE RAION PROMPT") < prompt.index("You are Fee Watcher.")
    assert prompt.index("You are Fee Watcher.") < prompt.index("MNDC total: 142")
    assert prompt.index("MNDC total: 142") < prompt.index("chat_id: 123")
    assert "AGENT ROLE" in prompt
    assert "AGENT MEMORY" in prompt


def test_build_agent_prompt_handles_empty_memory():
    prompt = build_agent_prompt(
        base_prompt="BASE",
        role_block="ROLE",
        memory_text="",
        trigger_context="CTX",
    )
    assert "BASE" in prompt and "ROLE" in prompt and "CTX" in prompt
    assert "AGENT MEMORY" not in prompt or "(no memory yet)" in prompt
