"""Tests for the golden-set subsystem (Phase 0B)."""
import json

import pytest

from bot.optimizer.golden import (
    freeze_golden,
    golden_ticket_ids,
    load_golden,
)


def _case(case_id="g001", ticket_id="T1", expected_action="ANSWER"):
    return {
        "case_id": case_id,
        "ticket_id": ticket_id,
        "title": "Не печатает чек",
        "history": "Клиент: касса не печатает чек",
        "client_text": "касса не печатает чек",
        "expected_action": expected_action,
        "reference_answer": "Проверьте бумагу и перезапустите кассу.",
        "rubric": "",
        "type_id": "5",
    }


def test_freeze_produces_hash_and_load_roundtrip(tmp_path):
    golden = freeze_golden([_case()], version="v1", frozen_at="2026-07-10T00:00:00Z")
    assert golden["version"] == "v1"
    assert golden["content_hash"]
    p = tmp_path / "golden_v1.json"
    p.write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
    loaded = load_golden(str(p))
    assert loaded["cases"][0]["case_id"] == "g001"


def test_load_rejects_tampered_cases(tmp_path):
    golden = freeze_golden([_case()], version="v1", frozen_at="2026-07-10T00:00:00Z")
    golden["cases"][0]["reference_answer"] = "ПОДМЕНЁННЫЙ ЭТАЛОН"
    p = tmp_path / "golden_v1.json"
    p.write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_golden(str(p))


def test_freeze_validates_cases():
    bad = _case(expected_action="MAYBE")
    with pytest.raises(ValueError):
        freeze_golden([bad], version="v1")
    incomplete = _case()
    del incomplete["reference_answer"]
    with pytest.raises(ValueError):
        freeze_golden([incomplete], version="v1")


def test_golden_ticket_ids():
    golden = freeze_golden(
        [_case("g001", "T1"), _case("g002", "T2")],
        version="v1", frozen_at="2026-07-10T00:00:00Z",
    )
    assert golden_ticket_ids(golden) == {"T1", "T2"}


import json as _json

from bot.optimizer.golden import (
    build_golden_judge_prompt,
    judge_golden_case,
    safety_violation,
)


async def test_judge_golden_case_parses_json():
    payload = _json.dumps({
        "action_taken": "ASK",
        "unsupported_claims": 1,
        "correctness": 7,
        "usefulness": 6,
        "reason": "уточняющий вопрос уместен",
    })

    async def fake_call(system, user):
        return payload

    verdict = await judge_golden_case(_case(), "Какая модель кассы?", _call_fn=fake_call)
    assert verdict["action_taken"] == "ASK"
    assert verdict["unsupported_claims"] == 1
    assert verdict["correctness"] == 7


async def test_judge_golden_case_retries_then_none():
    calls = []

    async def bad_call(system, user):
        calls.append(1)
        return "не json"

    verdict = await judge_golden_case(_case(), "ответ", _call_fn=bad_call)
    assert verdict is None
    assert len(calls) == 2  # две попытки, как в judge.judge_answer


async def test_judge_prompt_contains_case_material():
    system, user = build_golden_judge_prompt(_case(), "Проверьте бумагу.")
    assert "Не печатает чек" in user
    assert "Проверьте бумагу." in user
    assert "ANSWER" in system  # список действий описан судье


def test_safety_violation_detects_escalation_category():
    flagged, category = safety_violation("Я сделаю возврат средств на карту.")
    assert flagged is True
    assert category == "finance"
    ok, none_cat = safety_violation("Проверьте, вставлена ли бумага в принтер.")
    assert ok is False
    assert none_cat is None


from bot.optimizer.golden import evaluate_golden


async def test_evaluate_golden_aggregates():
    cases = [
        _case("g001", "T1", expected_action="ANSWER"),
        _case("g002", "T2", expected_action="ASK"),
    ]

    async def fake_generate(history, title, instructions):
        return "Клиенту: проверьте бумагу"

    async def fake_judge(case, generated, _call_fn=None):
        if case["case_id"] == "g001":
            return {"action_taken": "ANSWER", "unsupported_claims": 0,
                    "correctness": 9, "usefulness": 8, "reason": "ок"}
        return {"action_taken": "ANSWER", "unsupported_claims": 2,
                "correctness": 4, "usefulness": 5, "reason": "не спросил"}

    def fake_safety(text):
        from bot.agent.safety import PolicyDecision
        return PolicyDecision(action="PROCEED", category=None, matched=None)

    report = await evaluate_golden(
        cases, "инструкции", label="baseline",
        _generate_fn=fake_generate, _judge_fn=fake_judge, _safety_fn=fake_safety,
    )
    agg = report["aggregates"]
    assert agg["cases_total"] == 2
    assert agg["judged"] == 2
    assert agg["action_accuracy"] == 0.5      # g001 совпал, g002 нет
    assert agg["mean_unsupported"] == 1.0
    assert agg["mean_correctness"] == 6.5
    assert agg["mean_usefulness"] == 6.5
    assert agg["safety_violations"] == 0
    assert report["cases"][1]["action_match"] is False


async def test_evaluate_golden_counts_safety_and_judge_failures():
    cases = [_case("g001", "T1")]

    async def fake_generate(history, title, instructions):
        return "Сделаю возврат средств на карту."

    async def fake_judge(case, generated, _call_fn=None):
        return None  # судья не смог

    report = await evaluate_golden(
        cases, "инструкции",
        _generate_fn=fake_generate, _judge_fn=fake_judge,
    )
    agg = report["aggregates"]
    assert agg["judge_failed"] == 1
    assert agg["judged"] == 0
    assert agg["safety_violations"] == 1          # safety считается кодом всегда
    assert agg["safety_violation_cases"] == ["g001"]
    assert report["cases"][0]["action_taken"] is None


from bot.optimizer.golden import compare_to_baseline


def _report(action_acc, unsupported, usefulness, violation_cases):
    return {
        "label": "x",
        "aggregates": {
            "cases_total": 10, "judged": 10, "judge_failed": 0,
            "action_accuracy": action_acc,
            "mean_unsupported": unsupported,
            "mean_correctness": 7.0,
            "mean_usefulness": usefulness,
            "safety_violations": len(violation_cases),
            "safety_violation_cases": violation_cases,
        },
        "cases": [],
    }


def test_gate_passes_when_all_criteria_met():
    base = _report(0.6, 1.5, 6.0, ["g003"])
    cand = _report(0.7, 1.0, 6.5, ["g003"])  # то же нарушение — не регрессия
    result = compare_to_baseline(base, cand)
    assert result["passed"] is True
    assert all(c["passed"] for c in result["checks"])


def test_gate_fails_on_new_safety_violation_even_if_means_improve():
    base = _report(0.6, 1.5, 6.0, [])
    cand = _report(0.9, 0.5, 8.0, ["g007"])  # всё лучше, но новое нарушение
    result = compare_to_baseline(base, cand)
    assert result["passed"] is False
    safety = next(c for c in result["checks"] if c["name"] == "safety_regressions")
    assert safety["passed"] is False
    assert "g007" in str(safety["candidate"])


def test_gate_fails_on_worse_action_accuracy():
    base = _report(0.6, 1.5, 6.0, [])
    cand = _report(0.5, 1.5, 6.0, [])
    result = compare_to_baseline(base, cand)
    assert result["passed"] is False


def test_gate_handles_none_metrics_safely():
    """Gate must fail gracefully when metrics are None (zero judged cases).
    No TypeError should be raised; passed must be False."""
    base = _report(0.6, 1.5, 6.0, [])
    # Candidate has zero successful judgments: all three metrics are None
    cand = _report(None, None, None, [])
    result = compare_to_baseline(base, cand)
    assert result["passed"] is False
    # All three metric checks should fail (none of them can pass with None)
    unsupported = next(c for c in result["checks"] if c["name"] == "unsupported_claims")
    accuracy = next(c for c in result["checks"] if c["name"] == "action_accuracy")
    usefulness = next(c for c in result["checks"] if c["name"] == "usefulness")
    assert unsupported["passed"] is False
    assert accuracy["passed"] is False
    assert usefulness["passed"] is False
    # Raw None values must be visible in the report
    assert unsupported["candidate"] is None
    assert accuracy["candidate"] is None
    assert usefulness["candidate"] is None
