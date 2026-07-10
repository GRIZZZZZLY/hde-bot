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
