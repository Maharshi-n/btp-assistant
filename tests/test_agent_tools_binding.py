import pytest


def test_agent_only_tools_exist():
    from app.agents.agent_tools import AGENT_ONLY_TOOLS
    names = {t.name for t in AGENT_ONLY_TOOLS}
    assert {"agent_schedule_self", "agent_create_trigger", "agent_list_triggers",
            "agent_delete_trigger", "agent_recall"} <= names


def test_raion_tools_do_not_include_agent_tools():
    from app.agents.supervisor import SUPERVISOR_TOOLS
    names = {t.name for t in SUPERVISOR_TOOLS}
    assert "agent_schedule_self" not in names
    assert "agent_create_trigger" not in names


def test_tools_for_run_branches_on_agent_id():
    from app.agents.supervisor import _tools_for_run, SUPERVISOR_TOOLS
    raion = _tools_for_run(agent_id=None, mcp_tools=[])
    assert len(raion) == len(SUPERVISOR_TOOLS)
    raion_names = {t.name for t in raion}
    assert "agent_schedule_self" not in raion_names

    agent = _tools_for_run(agent_id=5, mcp_tools=[])
    agent_names = {t.name for t in agent}
    assert "agent_schedule_self" in agent_names
    # always-full tools decision: agent still has the full RAION set too
    assert "telegram_send" in agent_names


def test_agent_tools_are_auto():
    from app.permissions.policy import get_decision
    for n in ["agent_schedule_self", "agent_create_trigger", "agent_list_triggers",
              "agent_delete_trigger", "agent_recall"]:
        assert get_decision(n, {}) == "auto"


@pytest.mark.asyncio
async def test_agent_tool_without_context_is_safe():
    from app.agents.agent_tools import agent_list_triggers
    out = await agent_list_triggers.ainvoke({}, config={"configurable": {}})
    assert "no agent context" in out.lower()
