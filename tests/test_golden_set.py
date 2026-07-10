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


from types import SimpleNamespace

from bot.optimizer.golden import _strip_html, mine_candidates


def _post(pid, uid, text, is_comment=False):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text, is_comment=is_comment)


class _FakeHDE:
    def __init__(self, tickets, posts_by_ticket):
        self._tickets = tickets
        self._posts = posts_by_ticket

    async def get_closed_tickets_page(self, owner_id, page=1):
        return (self._tickets, 1) if page == 1 else ([], 1)

    async def get_ticket_posts(self, ticket_id, limit=20):
        return self._posts.get(str(ticket_id), [])


def test_strip_html():
    assert _strip_html("<p>касса <b>не</b> печатает</p>") == "касса не печатает"


async def test_mine_candidates_builds_cases():
    tickets = [
        {"id": "T1", "name": "Не печатает чек", "type_id": "5"},
        {"id": "T2", "name": "Вопрос по возврату", "type_id": "7"},
        {"id": "T3", "name": "Без ответа оператора", "type_id": "5"},
    ]
    posts = {
        "T1": [
            _post(1, "client9", "<p>касса не печатает чек</p>"),
            _post(2, "me", "Проверьте бумагу и перезапустите кассу."),
        ],
        "T2": [
            _post(3, "client9", "как сделать возврат средств покупателю?"),
            _post(4, "me", "Какая у вас модель кассы?"),
        ],
        "T3": [_post(5, "client9", "вопрос без ответа")],
    }
    cases = await mine_candidates(_FakeHDE(tickets, posts), "me", target=10)
    by_ticket = {c["ticket_id"]: c for c in cases}
    assert set(by_ticket) == {"T1", "T2"}          # T3 пропущен: нет ответа
    t1 = by_ticket["T1"]
    assert t1["reference_answer"] == "Проверьте бумагу и перезапустите кассу."
    assert "касса не печатает чек" in t1["history"]
    assert t1["expected_action"] == "ANSWER"
    t2 = by_ticket["T2"]
    assert t2["expected_action"] == "ESCALATE"     # policy-check: возврат средств
    assert cases[0]["case_id"] == "g001"


async def test_mine_candidates_respects_target():
    tickets = [{"id": f"T{i}", "name": f"t{i}", "type_id": "1"} for i in range(5)]
    posts = {
        f"T{i}": [_post(1, "c", f"вопрос {i}"), _post(2, "me", f"ответ {i}")]
        for i in range(5)
    }
    cases = await mine_candidates(_FakeHDE(tickets, posts), "me", target=3)
    assert len(cases) == 3


async def test_evaluate_golden_retries_rate_limit_then_succeeds():
    calls = {"n": 0}
    slept = []

    async def flaky_generate(history, title, instructions):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("Groq returned no choices: Rate limit reached (TPM)")
        return "Клиенту: перезагрузите кассу"

    async def ok_judge(case, generated, _call_fn=None):
        return {"action_taken": "ANSWER", "unsupported_claims": 0,
                "correctness": 8, "usefulness": 8, "reason": "ок"}

    async def noop_sleep(seconds):
        slept.append(seconds)

    report = await evaluate_golden(
        [_case("g001", "T1")], "инструкции",
        _generate_fn=flaky_generate, _judge_fn=ok_judge, _sleep=noop_sleep,
    )
    assert calls["n"] == 3                      # два ретрая, затем успех
    assert len(slept) == 2                      # два backoff-сна
    assert report["aggregates"]["generation_failed"] == 0
    assert report["aggregates"]["judged"] == 1


async def test_evaluate_golden_isolates_persistent_rate_limit():
    async def always_rate_limited(history, title, instructions):
        raise RuntimeError("429 rate limit: tokens per minute")

    async def noop_sleep(seconds):
        pass

    report = await evaluate_golden(
        [_case("g001", "T1"), _case("g002", "T2")], "инструкции",
        _generate_fn=always_rate_limited, _sleep=noop_sleep,
    )
    agg = report["aggregates"]
    assert agg["generation_failed"] == 2        # оба изолированы, не крэш
    assert agg["judged"] == 0
    assert agg["judge_failed"] == 0             # генерация не дошла до судьи
    assert report["cases"][0]["generation_failed"] is True


async def test_evaluate_golden_non_rate_limit_error_isolated_not_retried():
    calls = {"n": 0}

    async def boom(history, title, instructions):
        calls["n"] += 1
        raise ValueError("bad prompt")

    report = await evaluate_golden(
        [_case("g001", "T1")], "инструкции", _generate_fn=boom,
    )
    assert calls["n"] == 1                       # не-rate-limit не ретраится
    assert report["aggregates"]["generation_failed"] == 1


async def test_judge_golden_case_backs_off_on_rate_limit_then_succeeds():
    import json as _j
    calls = {"n": 0}
    slept = []

    async def flaky_call(system, user):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("429 rate limit: tokens per minute")
        return _j.dumps({"action_taken": "ANSWER", "unsupported_claims": 0,
                         "correctness": 9, "usefulness": 8, "reason": "ок"})

    async def noop_sleep(seconds):
        slept.append(seconds)

    verdict = await judge_golden_case(_case(), "ответ", _call_fn=flaky_call, _sleep=noop_sleep)
    assert verdict is not None and verdict["correctness"] == 9
    assert calls["n"] == 3          # 2 rate-limit + успех
    assert len(slept) == 2          # backoff на каждый rate-limit
