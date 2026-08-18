"""Решение по кандидату в базу знаний: «в базу» — статья, «мимо» — только статус.

Логика вынесена из хендлера: коллбэк Telegram остаётся тремя строками, а решение
проверяется тестами без подделки aiogram.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_kb_content(candidate: dict) -> str:
    """Статья в том же формате, что даёт кнопка «✏️ Исправить»: тема, диалог,
    правильный ответ. Эталон — то, что оператор реально написал клиенту."""
    parts = [f"Тема: {candidate.get('title') or ''}".strip()]
    history = (candidate.get("history") or "").strip()
    if history:
        parts.append(history)
    parts.append(f"Правильный ответ: {(candidate.get('reference_answer') or '').strip()}")
    return "\n\n".join(parts)


async def apply_kb_candidate(
    candidate_id: int, *, add: bool, _index_fn=None, _set_fn=None
) -> dict | None:
    """Проводит решение. None — если по кандидату уже решили (второй клик)."""
    if _set_fn is None:
        from ..db import set_kb_candidate_status as _set_fn
    row = await _set_fn(candidate_id, "added" if add else "skipped")
    if row is None:
        return None
    if not add:
        return row
    if _index_fn is None:
        from ..knowledge.indexer import index_knowledge_item as _index_fn
    await _index_fn(
        source="reconcile",
        content=build_kb_content(row),
        ticket_id=row.get("ticket_id"),
        title=row.get("title") or "",
        quality="corrected",
    )
    logger.info("KB candidate %s added from ticket %s", candidate_id, row.get("ticket_id"))
    return row
