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

_V2_RETRY_S = 20
# внешний провайдер v2: модели-«рассуждатели» тратят часть вывода на мысли,
# а потолка Groq в 1000 выходных токенов/мин там нет
_V2_EXTERNAL_MAX_TOKENS = 2000
_V2_EXTERNAL_TIMEOUT_S = 90


def build_ticket_context_block(context: dict) -> str:
    """Блок «что известно об этом тикете»: поля, вложения, звонок.

    Формулировка про вложения намеренно осторожная. Описание даёт Vision, а не
    глаз: «на скриншоте видно» превратило бы пересказ модели в наблюдение, и
    ошибка распознавания уехала бы клиенту как факт. Пустой блок при пустом
    контексте — промпт не должен пухнуть на типовом тикете.
    """
    parts = []
    facts = (context.get("ticket_facts") or "").strip()
    if facts:
        parts.append(f"Известно о тикете:\n{facts}")
    attachments = (context.get("attachments") or "").strip()
    if attachments:
        parts.append(
            "Описание вложений клиента (получено автоматически, может быть "
            f"неточным — ссылайся как «по описанию»):\n{attachments}"
        )
    call_notes = (context.get("call_notes") or "").strip()
    if call_notes:
        parts.append(
            "Что выяснили в звонке по этому тикету (это уже известно клиенту, "
            f"не переспрашивай):\n{call_notes}"
        )
    return "\n\n" + "\n\n".join(parts) if parts else ""


async def generate_agent_draft(
    context: dict,
    ticket_title: str,
    *,
    _call_fn=None,
    _prompt_fn=None,
    _format_fn=None,
    _sleep_fn=None,
) -> dict | None:
    if _call_fn is None:
        from ..ai_summary import call_groq_text as _call_fn
    if _prompt_fn is None:
        from ..ai_summary import _build_system_prompt as _prompt_fn
    if _format_fn is None:
        from ..ai_summary import get_active_format_instructions as _format_fn
    from ..config import config

    if config.agent_voice_v2_enabled:
        from .voice import build_prompt
        if _sleep_fn is None:
            import asyncio
            _sleep_fn = asyncio.sleep
        system = build_prompt(context, ticket_title)
        call_kwargs = dict(
            system=system, model=config.agent_draft_model,
            max_tokens=600, temperature=0.3,
            reasoning_effort=config.groq_reasoning_effort,
        )
        if config.agent_v2_llm_base_url:
            # другой OpenAI-совместимый провайдер только для v2: у него нет потолка
            # Groq в 1000 выходных токенов/мин, а reasoning_effort — параметр Groq
            call_kwargs.update(
                model=config.agent_v2_llm_model or config.agent_draft_model,
                max_tokens=_V2_EXTERNAL_MAX_TOKENS, reasoning_effort="",
                timeout_seconds=_V2_EXTERNAL_TIMEOUT_S,
                base_url=config.agent_v2_llm_base_url, api_key=config.agent_v2_llm_api_key,
            )
        raw = None
        for attempt in range(2):
            raw = await _call_fn(context["history"], **call_kwargs)
            if (raw or "").strip() or attempt == 1:
                break
            # ponytail: фиксированная пауза вместо x-ratelimit-reset-tokens —
            # call_groq_text не отдаёт заголовки; хватает, пока TPM-окно 60 с.
            await _sleep_fn(_V2_RETRY_S)
        draft = parse_agent_draft(raw or "")
        if draft is None:
            logger.warning("draft v2 parse failed; raw head: %s", (raw or "")[:200])
        return draft

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
    system += build_ticket_context_block(context)
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
    # max_tokens ≤ 1000: у Groq на qwen3.6-27b отдельный потолок OTPM (output
    # tokens per minute) = 1000, и запрос с бо́льшим max_tokens отклоняется
    # целиком с 429 «Request too large … on output tokens per minute», даже
    # когда ответ уместился бы в сотню токенов. Проверено на проде 2026-09-04:
    # 1200 роняет вызов, 800 проходит. Место для JSON освобождает не потолок, а
    # снятый блок <reasoning> выше.
    raw = await _call_fn(
        context["history"], system=system, model=config.agent_draft_model,
        max_tokens=800, temperature=0.3,
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
