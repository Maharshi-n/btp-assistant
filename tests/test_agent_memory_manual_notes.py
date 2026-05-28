import pytest

from app.agents import agent_memory as mem


def test_split_and_join_roundtrip():
    text = ("## Durable facts\na\n"
            "<!-- MANUAL_NOTES_BEGIN -->\nmy hand notes\n<!-- MANUAL_NOTES_END -->\n")
    auto, manual = mem.split_manual_notes(text)
    assert "my hand notes" in manual
    assert "MANUAL_NOTES" not in auto
    joined = mem.join_manual_notes("## Durable facts\nNEW\n", manual)
    assert "NEW" in joined
    assert "my hand notes" in joined


def test_split_missing_markers_treats_all_as_auto():
    text = "## Durable facts\njust auto, no markers\n"
    auto, manual = mem.split_manual_notes(text)
    assert "just auto" in auto
    assert manual == ""


@pytest.mark.asyncio
async def test_distil_preserves_manual_block(tmp_path, monkeypatch):
    p = tmp_path / "1_memory.md"
    p.write_text("## Durable facts\nold fact\n"
                 "<!-- MANUAL_NOTES_BEGIN -->\nDO NOT LOSE THIS\n<!-- MANUAL_NOTES_END -->\n",
                 encoding="utf-8")

    async def fake_llm(old_memory, new_messages, cap_lines, source="trigger"):
        # The distiller must NOT see the manual block, and returns only auto sections.
        assert "DO NOT LOSE THIS" not in old_memory
        return "## Durable facts\nnew distilled fact\n"

    monkeypatch.setattr(mem, "_call_memory_llm", fake_llm)
    await mem.distil_memory(1, p, "[user] something happened")
    result = p.read_text(encoding="utf-8")
    assert "new distilled fact" in result
    assert "DO NOT LOSE THIS" in result  # preserved verbatim
