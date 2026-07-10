"""Черновая генерация агента: базовый system-промпт ai_summary + инструкция
выбора действия; модель — из config.agent_draft_model."""
from __future__ import annotations

from .actions import build_action_instruction, parse_agent_draft


async def generate_agent_draft(
    context: dict,
    ticket_title: str,
    *,
    _call_fn=None,
    _prompt_fn=None,
    _format_fn=None,
) -> dict | None:
    if _call_fn is None:
        from ..ai_summary import call_groq_text as _call_fn
    if _prompt_fn is None:
        from ..ai_summary import _build_system_prompt as _prompt_fn
    if _format_fn is None:
        from ..ai_summary import get_active_format_instructions as _format_fn
    from ..config import config

    rag_examples = [
        e["used_excerpt"] for e in context.get("evidence", [])
        if e["source_type"] == "knowledge_item"
    ] or None
    format_instructions = await _format_fn()
    base_system = _prompt_fn(
        ticket_title,
        rag_examples=rag_examples,
        wiki_context=context.get("wiki"),
        equipment=context.get("equipment"),
        solution_steps=context.get("solution_steps"),
        format_instructions=format_instructions,
    )
    system = base_system + build_action_instruction()
    raw = await _call_fn(
        context["history"], system=system, model=config.agent_draft_model,
        max_tokens=800, temperature=0.3,
    )
    return parse_agent_draft(raw or "")
