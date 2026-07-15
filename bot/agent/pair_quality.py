"""LLM-фильтр качества dialogue_pairs (Phase 2B).

Закрытый тикет ≠ правильный ответ: в few-shot идут только пары, прошедшие
фильтр (auto_accepted). Отсев: приветствия/служебные без решения, «позвоните
нам», промежуточные реплики, устаревшие инструкции. None-вердикт (rate-limit,
мусорный JSON) оставляет пару unreviewed — доберётся следующим прогоном."""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_STATUSES = ("auto_accepted", "rejected", "outdated")


def _build_gate_prompt(pair: dict) -> tuple[str, str]:
    system = (
        "Ты оцениваешь, годится ли ответ оператора техподдержки кассового ПО "
        "как ОБРАЗЕЦ для обучения ассистента (few-shot пример).\n"
        "auto_accepted — конкретное решение/уточнение по существу: есть действие, "
        "модель/ПО, путь или конкретный вопрос.\n"
        "rejected — приветствие, служебная реплика, «позвоните нам» без решения, "
        "«ожидайте», пустое подтверждение, фрагмент без смысла.\n"
        "outdated — инструкция, явно привязанная к устаревшей версии/процессу.\n"
        'Верни СТРОГО JSON: {"status":"auto_accepted|rejected|outdated",'
        '"reason":"кратко по-русски"}'
    )
    user = (
        f"Диалог до ответа:\n{pair['context'][-1500:]}\n\n"
        f"Ответ оператора (кандидат в образцы):\n{pair['operator_answer'][:800]}\n\n"
        f"Дата ответа: {pair.get('operator_answer_at') or 'неизвестна'}"
    )
    return system, user


async def judge_pair_quality(pair: dict, *, _call_fn=None) -> tuple[str, str] | None:
    if _call_fn is None:
        from ..ai_summary import call_groq_json as _call_fn
    from ..config import config

    system, user = _build_gate_prompt(pair)
    try:
        raw = await _call_fn(system, user, model=config.agent_selfcheck_model)
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("status") not in _STATUSES:
        return None
    return obj["status"], str(obj.get("reason", ""))[:200]


async def gate_pending_pairs(
    limit: int = 50, *, _judge_fn=None, _list_fn=None, _set_fn=None,
    _sleep_fn=None, pause_s: float = 6.0, retry_pause_s: float = 30.0,
) -> dict:
    """Батч-фильтр: размечает до limit непроверенных пар. Не падает на сбоях.

    Пейсинг под Groq free-tier (TPM 12k, ~1k токенов/вызов): pause_s между
    парами; None-вердикт (rate-limit/мусорный JSON) → retry_pause_s и один
    повтор, только потом skipped."""
    if _judge_fn is None:
        _judge_fn = judge_pair_quality
    if _list_fn is None:
        from ..db import list_pairs_for_gating as _list_fn
    if _set_fn is None:
        from ..db import set_pair_quality as _set_fn
    if _sleep_fn is None:
        import asyncio
        _sleep_fn = asyncio.sleep
    from ..config import config

    stats = {"gated": 0, "accepted": 0, "rejected": 0, "outdated": 0, "skipped": 0}
    pairs = await _list_fn(limit, own_operator_id=str(config.hde_owner_id))
    for i, pair in enumerate(pairs):
        if i and pause_s:
            await _sleep_fn(pause_s)
        verdict = await _judge_fn(pair)
        if verdict is None:
            await _sleep_fn(retry_pause_s)
            verdict = await _judge_fn(pair)
        if verdict is None:
            stats["skipped"] += 1
            continue
        status, reason = verdict
        await _set_fn(pair["pair_id"], status, reason)
        stats["gated"] += 1
        key = {"auto_accepted": "accepted", "rejected": "rejected",
               "outdated": "outdated"}[status]
        stats[key] += 1
    if stats["gated"]:
        try:
            from .pair_retrieval import invalidate_pairs_cache
            invalidate_pairs_cache()
        except ImportError:
            pass  # Task 3 ещё не в дереве — безопасно при поэтапной сборке
    return stats
