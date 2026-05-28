from app.agents import supervisor


def test_agent_base_excludes_hijack_rules():
    ab = supervisor._agent_base_system_prompt()
    # The proactive "go query the DB / web for any question" rules must NOT be in
    # the agent base — they hijacked agents into answering email questions.
    assert "check the database first" not in ab
    assert "almost always database questions" not in ab
    # It must affirmatively tell the agent to stay in its role.
    assert "STAY IN YOUR ROLE" in ab


def test_raion_base_is_untouched():
    # RAION's own prompt must KEEP its database-first behaviour.
    rb = supervisor._supervisor_system_prompt()
    assert "check the database first" in rb


def test_agent_base_keeps_raion_flavour():
    ab = supervisor._agent_base_system_prompt()
    # Still has identity, tools, channel discipline, no-narration — the good parts.
    assert "telegram_send" in ab
    assert "INVOKE" in ab or "call tools" in ab.lower()
    assert "WORKSPACE" in ab or "Workspace" in ab
