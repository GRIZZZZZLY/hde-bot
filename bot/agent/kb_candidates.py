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


async def resolve_kb_conflict_decision(
    candidate_id: int, *, keep_new: bool,
    _resolve_fn=None, _index_fn=None, _retire_fn=None,
) -> dict | None:
    """Решение человека по противоречию правил.

    keep_new — новое правило пишется в базу, а опровергнутое снимается с поиска
    (quality='archived', строка остаётся для разбора). keep_new=False оставляет
    базу как есть. None — по кандидату уже решили.
    """
    import json

    if _resolve_fn is None:
        from ..db import resolve_kb_conflict as _resolve_fn
    row = await _resolve_fn(candidate_id, "added" if keep_new else "skipped")
    if row is None or not keep_new:
        return row

    try:
        rule = json.loads(row.get("rule_json") or "{}")
    except Exception:
        rule = {}
    if not rule.get("rule"):
        logger.warning("kb conflict %s: выжимка потеряна, правило не записано", candidate_id)
        return row

    from .kb_distill import RULE_QUALITY, RULE_SOURCE, build_rule_content, rule_content_hash
    if _index_fn is None:
        from ..knowledge.indexer import index_knowledge_item as _index_fn
    if _retire_fn is None:
        from ..db import retire_knowledge_item as _retire_fn

    await _index_fn(
        source=RULE_SOURCE,
        content=build_rule_content(rule, ticket_id=row.get("ticket_id") or ""),
        ticket_id=row.get("ticket_id") or "",
        title=(rule.get("symptom") or "")[:120],
        quality=RULE_QUALITY,
        content_hash=rule_content_hash(rule),
    )
    old_id = row.get("conflict_item_id")
    if old_id:
        await _retire_fn(int(old_id))
    logger.info("kb conflict %s resolved: новое правило принято", candidate_id)
    return row


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
