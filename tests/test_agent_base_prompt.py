from app.agents import supervisor


def test_agent_base_excludes_hijack_rules():
    ab = supervisor._agent_base_system_prompt()
    # The proactive "go query the DB / web for any question" rules must NOT be in
    # the agent base — they hijacked agents into answering email questions.
    assert "check the database first" not in ab
    assert "almost always database questions" not in ab
    # It must affirmatively tell the agent to stay in its role on autonomous runs.
    assert "ONLY what your AGENT ROLE says" in ab


def test_agent_base_stay_in_role_is_scoped_to_autonomous_runs():
    ab = supervisor._agent_base_system_prompt()
    # The "stay in your role / don't act on the data" rule must be scoped to
    # autonomous [AGENT RUN] fires, and must NOT override a direct user instruction.
    # Otherwise '/switch into agent -> reply to the email' gets ignored and the
    # agent just re-notifies. The block must say a direct user message wins.
    assert "[AGENT RUN]" in ab
    low = ab.lower()
    assert "directly" in low and "follow" in low  # direct user instruction is followed
    assert "override" in low or "overrides" in low


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
