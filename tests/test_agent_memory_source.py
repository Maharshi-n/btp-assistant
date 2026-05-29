from app.agents import agent_memory


def test_memory_prompt_records_events_not_behavioural_rules():
    p = agent_memory._MEMORY_SYSTEM_PROMPT.lower()
    # Memory must capture the agent's environment (what happened around it)...
    assert "record" in p or "what happened" in p or "environment" in p
    # ...but must NEVER become a standing instruction / behavioural pattern that makes
    # the agent auto-act on future similar events (the 'Maharshi always replies like
    # this -> auto-reply to the next similar mail' poisoning bug).
    assert "not an instruction" in p or "not a rule" in p or "do not auto" in p or "never auto" in p
    assert "pattern" in p


async def test_distil_passes_source_to_llm(tmp_path, monkeypatch):
    target = tmp_path / "m.md"
    target.write_text("## Durable facts\n", encoding="utf-8")
    seen = {}

    async def fake_llm(old, new, cap, source="trigger"):
        seen["source"] = source
        return "## Durable facts\n- kept"

    monkeypatch.setattr(agent_memory, "_call_memory_llm", fake_llm)

    await agent_memory.distil_memory(1, target, "hi", source="live_chat")
    assert seen["source"] == "live_chat"

    await agent_memory.distil_memory(1, target, "cron ran")  # default
    assert seen["source"] == "trigger"


async def test_call_memory_llm_labels_source(monkeypatch):
    captured = {}

    class _Msg:
        content = "## Durable facts\n- x"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **kw):
            captured["user"] = kw["messages"][1]["content"]
            captured["model"] = kw["model"]
            captured["has_tools"] = "tools" in kw
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr(agent_memory, "AsyncOpenAI", lambda **kw: _Client())

    await agent_memory._call_memory_llm("old", "new events", 200, source="live_chat")
    assert "LIVE CHAT" in captured["user"]
    assert captured["model"] == "gpt-4o-mini"
    # Safety: the memory model must never have tools bound.
    assert captured["has_tools"] is False
