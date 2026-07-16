"""Self-check одним LLM-вызовом ПРОТИВ СОДЕРЖИМОГО источников (used_excerpt),
а не их названий. Консервативный дефолт unsupported→ASK, никогда не падает."""
from __future__ import annotations

import json

_DEFAULT = {"status": "unsupported", "fallback_action": "ASK", "fallback_client_text": ""}
_STATUSES = ("supported", "partially_supported", "unsupported")


def _render_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return "Источники не найдены."
    lines = []
    for i, e in enumerate(evidence, 1):
        label = e.get("source_type", "?")
        lines.append(f"[{i}] ({label}) {e.get('used_excerpt', '')}")
    return "\n".join(lines)


def _build_selfcheck_prompt(client_text, generated_client, evidence, history):
    system = (
        "Ты проверяешь ответ техподдержки кассового ПО на опору в источниках. "
        "Каждое фактическое утверждение ответа должно подтверждаться историей "
        "тикета ИЛИ приведёнными фрагментами источников. Выдуманные шаги, модели, "
        "настройки — нарушение.\n"
        "status: supported — все утверждения подтверждены; partially_supported — "
        "часть без опоры; unsupported — ключевые утверждения не подтверждены.\n"
        "Если status != supported, предложи безопасный fallback: ASK (уточнить у "
        "клиента) или ESCALATE (передать оператору) и текст fallback_client_text.\n"
        "fallback_client_text НЕ должен спрашивать то, что клиент уже сообщил в "
        "вопросе или истории (перечитай их перед формулировкой). Если нужные факты "
        "уже даны — предложи следующий диагностический шаг или выбери ESCALATE.\n"
        'Верни СТРОГО JSON: {"status":"...","fallback_action":"ASK|ESCALATE",'
        '"fallback_client_text":"..."}'
    )
    user = (
        f"Вопрос клиента: {client_text}\n\n"
        f"История:\n{history}\n\n"
        f"Фрагменты источников:\n{_render_evidence(evidence)}\n\n"
        f"Проверяемый ответ клиенту:\n{generated_client}"
    )
    return system, user


async def self_check(
    client_text: str,
    generated_client: str,
    evidence: list[dict],
    history: str,
    *,
    _call_fn=None,
) -> dict:
    if _call_fn is None:
        from ..ai_summary import call_groq_json as _call_fn
    from ..config import config

    system, user = _build_selfcheck_prompt(client_text, generated_client, evidence, history)
    try:
        raw = await _call_fn(system, user, model=config.agent_selfcheck_model)
        obj = json.loads(raw)
    except Exception:
        return dict(_DEFAULT)
    if not isinstance(obj, dict) or obj.get("status") not in _STATUSES:
        return dict(_DEFAULT)
    action = obj.get("fallback_action")
    if action not in ("ASK", "ESCALATE"):
        action = "ASK"
    return {
        "status": obj["status"],
        "fallback_action": action,
        "fallback_client_text": str(obj.get("fallback_client_text", "")),
    }
