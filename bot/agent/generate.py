"""Черновая генерация агента: базовый system-промпт ai_summary + инструкция
выбора действия; модель — из config.agent_draft_model."""
from __future__ import annotations

import logging

from .actions import (
    build_action_instruction,
    build_json_override,
    parse_agent_draft,
    strip_reasoning_directive,
)

logger = logging.getLogger(__name__)


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
    # Блок <reasoning> из формат-инструкций несовместим с JSON-выводом: модель
    # пишет рассуждение, оно съедает max_tokens, JSON обрывается и драфт
    # молча уходит на legacy. Правила стиля из тех же инструкций остаются.
    format_instructions = strip_reasoning_directive(await _format_fn())
    base_system = _prompt_fn(
        ticket_title,
        rag_examples=rag_examples,
        wiki_context=context.get("wiki"),
        equipment=context.get("equipment"),
        solution_steps=context.get("solution_steps"),
        format_instructions=format_instructions,
    )
    system = base_system + build_action_instruction()
    pair_examples = [
        d["used_excerpt"] for d in context.get("demos", [])
        if d.get("source_type") == "dialogue_pair"
    ]
    if pair_examples:
        block = "\n\nПримеры, как оператор решал похожие обращения (следуй их стилю и конкретике):"
        for i, ex in enumerate(pair_examples, 1):
            block += f"\nПример {i}:\n{ex}"
        system += block
    # JSON-override ПОСЛЕДНИМ — после few-shot (прод-инцидент 2026-07-12)
    system += build_json_override()
    raw = await _call_fn(
        context["history"], system=system, model=config.agent_draft_model,
        max_tokens=1200, temperature=0.3,
        reasoning_effort=config.groq_reasoning_effort,
    )
    draft = parse_agent_draft(raw or "")
    if draft is None:
        # Две разные болезни под одним симптомом: пустой ответ — это отказ
        # провайдера (429/413), текст без JSON — модель проигнорировала
        # override. Раньше обе писались одной строкой и не разделялись в логе.
        if not (raw or "").strip():
            logger.warning("draft parse failed: пустой ответ модели (429/413?)")
        else:
            logger.warning("draft parse failed; raw head: %s", raw[:200])
    return draft
