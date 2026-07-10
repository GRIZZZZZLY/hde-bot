"""Golden set (Phase 0B): неизменяемый эталонный датасет для baseline-оценки.

Кейсы майнятся из закрытых тикетов HDE, валидируются оператором вручную,
замораживаются с content_hash. Оценка — мульти-осевая (см. evaluate_golden),
gate против baseline — compare_to_baseline. Никакого влияния на рантайм бота.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

ACTIONS = ("ANSWER", "ASK", "ESCALATE", "NO_ACTION")

CASE_KEYS = (
    "case_id", "ticket_id", "title", "history", "client_text",
    "expected_action", "reference_answer", "rubric", "type_id",
)

_REQUIRED_NONEMPTY = ("case_id", "ticket_id", "history", "reference_answer")


def _content_hash(cases: list[dict]) -> str:
    canonical = json.dumps(cases, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_case(case: dict) -> None:
    missing = [k for k in CASE_KEYS if k not in case]
    if missing:
        raise ValueError(f"case {case.get('case_id', '?')}: missing keys {missing}")
    if case["expected_action"] not in ACTIONS:
        raise ValueError(
            f"case {case['case_id']}: expected_action must be one of {ACTIONS}"
        )
    for key in _REQUIRED_NONEMPTY:
        if not str(case[key]).strip():
            raise ValueError(f"case {case.get('case_id', '?')}: empty {key}")


def freeze_golden(
    cases: list[dict], version: str, frozen_at: str | None = None
) -> dict:
    """Валидирует кейсы и возвращает неизменяемый снапшот с content_hash."""
    if not cases:
        raise ValueError("golden set is empty")
    for case in cases:
        _validate_case(case)
    return {
        "version": version,
        "frozen_at": frozen_at or datetime.now(timezone.utc).isoformat(),
        "content_hash": _content_hash(cases),
        "cases": cases,
    }


def load_golden(path: str) -> dict:
    """Читает замороженный golden set; ValueError при подмене содержимого."""
    with open(path, encoding="utf-8") as f:
        golden = json.load(f)
    actual = _content_hash(golden["cases"])
    if actual != golden.get("content_hash"):
        raise ValueError(
            f"golden set hash mismatch: stored {golden.get('content_hash')!r}, "
            f"actual {actual!r} — dataset was modified after freeze"
        )
    return golden


def golden_ticket_ids(golden: dict) -> set[str]:
    """Список ticket_id для исключения из retrieval/few-shot (фазы 1/2B)."""
    return {str(c["ticket_id"]) for c in golden["cases"]}


def build_golden_judge_prompt(case: dict, generated: str) -> tuple[str, str]:
    system = (
        "Ты — строгий судья качества ответов техподдержки кассового ПО. "
        "Сравни ответ кандидата с историей тикета и эталонным ответом оператора.\n"
        "Классифицируй действие кандидата (action_taken): "
        "ANSWER — даёт решение; ASK — задаёт уточняющие вопросы; "
        "ESCALATE — отказывается решать сам и передаёт оператору; "
        "NO_ACTION — отвечает, что ответ не требуется.\n"
        "unsupported_claims — число фактических утверждений кандидата, "
        "которые НЕ подтверждаются ни историей тикета, ни эталонным ответом.\n"
        "correctness — 0-10, совпадение по сути с эталоном. "
        "usefulness — 0-10, полезность для клиента.\n"
        'Ответь строго JSON: {"action_taken": "...", "unsupported_claims": N, '
        '"correctness": N, "usefulness": N, "reason": "кратко по-русски"}'
    )
    rubric = f"\nКритерии оператора: {case['rubric']}" if case.get("rubric") else ""
    user = (
        f"Тема: {case['title']}\n\n"
        f"История тикета:\n{case['history']}\n\n"
        f"Эталонный ответ оператора:\n{case['reference_answer']}\n\n"
        f"Ожидаемое действие: {case['expected_action']}{rubric}\n\n"
        f"Ответ кандидата:\n{generated}"
    )
    return system, user


async def judge_golden_case(
    case: dict, generated: str, *, _call_fn=None
) -> dict | None:
    """Мульти-осевой вердикт судьи; None если LLM дважды вернул невалидный JSON."""
    if _call_fn is None:
        from .judge import _call_groq_judge
        _call_fn = _call_groq_judge
    system, user = build_golden_judge_prompt(case, generated)
    for _ in range(2):
        try:
            raw = await _call_fn(system, user)
            verdict = json.loads(raw)
        except Exception:
            continue
        if (
            isinstance(verdict, dict)
            and verdict.get("action_taken") in ACTIONS
            and isinstance(verdict.get("unsupported_claims"), int)
            and isinstance(verdict.get("correctness"), int)
            and isinstance(verdict.get("usefulness"), int)
        ):
            return verdict
    return None


def safety_violation(generated: str, *, _safety_fn=None) -> tuple[bool, str | None]:
    """Код-детерминированная ось безопасности: текст ответа сам предлагает
    действие эскалационной категории (возврат денег, перерегистрация ККТ,
    удаление данных, доступы) — нарушение независимо от ожидаемого действия."""
    if _safety_fn is None:
        from ..agent.safety import post_generation_safety_check
        _safety_fn = post_generation_safety_check
    decision = _safety_fn(generated)
    if decision.action == "ESCALATE":
        return True, decision.category
    return False, None
