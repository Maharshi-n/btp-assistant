"""Compose an agent's system prompt from labelled, ordered layers.

Layer order (priority): RAION base -> agent role -> agent memory -> trigger context.
Specialization lives in the role + memory layers; the base is NEVER forked.
"""
from __future__ import annotations


def build_agent_prompt(
    base_prompt: str,
    role_block: str,
    memory_text: str,
    trigger_context: str,
) -> str:
    parts = [base_prompt.rstrip()]

    parts.append(
        "\n\n━━━ AGENT ROLE ━━━\n"
        + role_block.strip()
        + "\nYou operate autonomously on triggers; you are NOT in a live chat with the user."
    )

    if memory_text.strip():
        parts.append("\n\n━━━ AGENT MEMORY ━━━\n" + memory_text.strip())

    if trigger_context.strip():
        parts.append("\n\n" + trigger_context.strip())

    return "".join(parts)
