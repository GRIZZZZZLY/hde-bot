"""Судья v2: оси 1–20, N/A, некомпенсируемые caps, k повторов, кеш вердиктов.

Чеклист из спеки §11.11. Что здесь проверяется по существу:

- нормировка `(s-1)/19`, а не `s/20` — иначе «полностью неверно» = 0.05;
- `applicable=false` исключает ось из ЗНАМЕНАТЕЛЯ, а не считается нейтральной;
- неверный диагноз не отмывается стилем и форматом;
- битый вердикт выпадает из усреднения, а не проезжает нулём или дефолтом;
- ключ кеша меняется от индекса повтора, модели, рубрики и температуры.
"""
import json

import pytest

from bot.optimizer import judge
from bot.optimizer.checks import CheckResult
from bot.optimizer.judge import (
    AXES,
    apply_caps,
    aggregate_verdict,
    axis_stats,
    build_judge_prompt,
    holdout_detail,
    holdout_score,
    judge_axes,
    judge_case,
    normalize,
    parse_verdict,
    verdict_cache_key,
)


def _axis(score, applicable=True, reason="ок"):
    return {"score": score, "applicable": applicable, "reason": reason}


def _verdict(diagnosis=20, equipment=20, actionability=20, human=20):
    """Балл или готовый словарь оси (для N/A: _axis(None, False))."""
    def wrap(value):
        return value if isinstance(value, dict) else _axis(value)

    return json.dumps({
        "diagnosis": wrap(diagnosis),
        "equipment": wrap(equipment),
        "actionability": wrap(actionability),
        "human": wrap(human),
    })


# --- рубрика ---------------------------------------------------------------

def test_prompt_has_axes_anchors_and_na_contract():
    system, user = build_judge_prompt(
        history="Касса Атол 30Ф не печатает чек",
        title="Не печатает",
        candidate="Клиенту: перезагрузите кассу",
        op_answer="Перезагрузите кассу, проверьте ленту",
    )
    assert "Атол 30Ф" in user
    assert "перезагрузите кассу" in user.lower()
    for axis in AXES:
        assert axis in system
    assert "20 — корректно и готово к отправке" in system  # якоря
    assert "applicable=false" in system
    # формат/длину/безопасность считает код — судья не должен их оценивать
    assert "код" in system and "не оценивай" in system


def test_prompt_forbids_equipment_na_for_invented_model():
    system, _ = build_judge_prompt("h", "t", "c", "o")
    assert "выдумка" in system and "НЕ equipment=false" in system


# --- нормировка и свёртка ---------------------------------------------------

def test_normalize_maps_scale_to_unit():
    assert normalize(1) == 0.0
    assert normalize(20) == 1.0
    assert normalize(10.5) == pytest.approx(0.5)


def test_aggregate_weighted_mean():
    verdict = parse_verdict(_verdict(diagnosis=20, equipment=1, actionability=20, human=1))
    # веса: diagnosis .40 + actionability .30 = 0.70 на единицах, .15+.15 на нулях
    assert aggregate_verdict(verdict) == pytest.approx(0.70)


def test_na_axis_leaves_denominator():
    """Неприменимая ось не тянет балл к середине — она просто не считается."""
    verdict = parse_verdict(
        _verdict(diagnosis=20, actionability=20, human=20, equipment=_axis(None, False))
    )
    assert aggregate_verdict(verdict) == pytest.approx(1.0)


def test_aggregate_none_when_nothing_applicable():
    verdict = parse_verdict(_verdict(
        diagnosis=_axis(None, False), equipment=_axis(None, False),
        actionability=_axis(None, False), human=_axis(None, False),
    ))
    assert aggregate_verdict(verdict) is None


# --- валидация вердикта -----------------------------------------------------

@pytest.mark.parametrize("raw", [
    "не json",
    json.dumps({"diagnosis": _axis(20)}),                         # оси пропущены
    _verdict(diagnosis="хорошо"),                                 # строка вместо числа
    _verdict(diagnosis=21),                                       # вне 1..20
    _verdict(diagnosis=0),                                        # вне 1..20
    json.dumps({axis: _axis(10) for axis in AXES} | {"human": {"score": 10}}),  # нет applicable
])
def test_parse_rejects_broken_verdicts(raw):
    assert parse_verdict(raw) is None


def test_parse_accepts_na_without_score():
    verdict = parse_verdict(_verdict(equipment=_axis(None, False)))
    assert verdict["equipment"]["applicable"] is False
    assert verdict["equipment"]["score"] is None


# --- caps -------------------------------------------------------------------

def test_bad_diagnosis_cannot_be_washed_by_style():
    """Неверный факт с идеальным стилем и форматом всё равно не проходит."""
    verdict = parse_verdict(_verdict(diagnosis=3, equipment=20, actionability=20, human=20))
    score = aggregate_verdict(verdict)
    stats = axis_stats([verdict])
    capped = apply_caps(score, stats, CheckResult())
    assert score > 0.6           # среднее по осям выглядит прилично
    assert capped <= judge.DIAGNOSIS_FAIL_CAP  # но потолок опущен


def test_fatal_hard_check_zeroes_case():
    verdict = parse_verdict(_verdict())
    stats = axis_stats([verdict])
    assert apply_caps(1.0, stats, CheckResult(fatal="safety")) == 0.0


def test_length_penalty_multiplies():
    verdict = parse_verdict(_verdict())
    stats = axis_stats([verdict])
    assert apply_caps(1.0, stats, CheckResult(penalty=0.8)) == pytest.approx(0.8)


# --- k повторов -------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeats_are_averaged_exactly():
    scores = iter([20, 10, 1])  # нормированные: 1.0, ~0.474, 0.0

    async def fake_call(system, user, *, temperature):
        value = next(scores)
        return _verdict(diagnosis=value, equipment=value, actionability=value, human=value)

    result = await judge_axes("h", "t", "cand", "ref", repeats=3, pause_s=0, _call_fn=fake_call)

    expected = (normalize(20) + normalize(10) + normalize(1)) / 3
    assert result["score"] == pytest.approx(expected)
    assert len(result["verdicts"]) == 3
    assert result["spread"] == pytest.approx(1.0)  # разброс сохранён
    assert result["invalid"] == 0


@pytest.mark.asyncio
async def test_one_broken_repeat_of_three_survives():
    answers = iter([_verdict(diagnosis=20), "мусор", _verdict(diagnosis=20)])

    async def fake_call(system, user, *, temperature):
        return next(answers)

    result = await judge_axes("h", "t", "c", "r", repeats=3, pause_s=0, _call_fn=fake_call)

    assert result["invalid"] == 1
    assert len(result["verdicts"]) == 2
    assert result["score"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_all_repeats_broken_gives_none_not_zero():
    """Ноль означал бы «ответ плохой»; на самом деле судья не ответил."""
    async def fake_call(system, user, *, temperature):
        return "мусор"

    result = await judge_axes("h", "t", "c", "r", repeats=2, pause_s=0, _call_fn=fake_call)
    assert result["score"] is None
    assert result["invalid"] == 2


@pytest.mark.asyncio
async def test_non_rate_limit_error_counted_as_invalid_without_retry():
    calls = []

    async def fake_call(system, user, *, temperature):
        calls.append(1)
        raise RuntimeError("bad gateway")

    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    result = await judge_axes(
        "h", "t", "c", "r", repeats=2, pause_s=0, _call_fn=fake_call, _sleep=no_sleep
    )
    assert result["score"] is None and result["invalid"] == 2
    assert len(calls) == 2   # по одному разу на повтор, без ретраев
    assert slept == []


@pytest.mark.asyncio
async def test_rate_limit_is_retried_not_counted_as_broken_verdict():
    """429 — это «минута исчерпана», а не «модель не смогла оценить».

    Без ретрая free-tier превращал почти каждый прогон в набор битых вердиктов
    (замер на проде: судья терял оценки на втором же кейсе).
    """
    attempts = []
    slept = []

    async def flaky_call(system, user, *, temperature):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError(
                "Rate limit reached for model ... on tokens per minute (TPM): Limit 8000"
            )
        return _verdict()

    async def no_sleep(seconds):
        slept.append(seconds)

    result = await judge_axes(
        "h", "t", "c", "r", repeats=1, pause_s=0, _call_fn=flaky_call, _sleep=no_sleep
    )

    assert result["invalid"] == 0
    assert result["score"] == pytest.approx(1.0)
    assert len(attempts) == 2 and slept  # подождали и переспросили


@pytest.mark.asyncio
async def test_pacing_sleeps_between_repeats():
    """Один кейс при k=3 — около 7000 токенов при лимите 8000/мин."""
    slept = []

    async def fake_call(system, user, *, temperature):
        return _verdict()

    async def no_sleep(seconds):
        slept.append(seconds)

    await judge_axes(
        "h", "t", "c", "r", repeats=3, pause_s=3.0, _call_fn=fake_call, _sleep=no_sleep
    )
    assert slept == [3.0, 3.0]   # между повторами, но не перед первым


# --- кеш вердиктов ----------------------------------------------------------

def _key(**kw):
    base = dict(
        candidate="cand", reference="ref", judge_model="m",
        temperature=0.6, reasoning_effort="", repeat_index=0,
        style_guide="guide",
    )
    base.update(kw)
    return verdict_cache_key(**base)


def test_cache_key_differs_per_repeat():
    keys = {_key(repeat_index=i) for i in range(3)}
    assert len(keys) == 3


@pytest.mark.parametrize("field,value", [
    ("judge_model", "other-model"),
    ("rubric_version", "v3"),
    ("temperature", 0.2),
    ("reasoning_effort", "low"),
    ("schema_version", "2"),
    ("style_guide", "другой стайлгайд"),
    ("candidate", "другой ответ"),
    ("reference", "другой эталон"),
])
def test_cache_key_invalidated_by(field, value):
    assert _key() != _key(**{field: value})


@pytest.mark.asyncio
async def test_cache_hit_skips_call():
    store = {}

    class _Cache:
        def get(self, key):
            return store.get(key)

        def put(self, key, value):
            store[key] = value

    calls = []

    async def fake_call(system, user, *, temperature):
        calls.append(1)
        return _verdict()

    cache = _Cache()
    first = await judge_axes("h", "t", "c", "r", repeats=2, pause_s=0, _call_fn=fake_call, cache=cache)
    second = await judge_axes("h", "t", "c", "r", repeats=2, pause_s=0, _call_fn=fake_call, cache=cache)

    assert len(calls) == 2          # только первый прогон дошёл до модели
    assert second["score"] == first["score"]


# --- кейс целиком -----------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_case_skips_judge_on_fatal_check():
    async def fake_call(system, user, *, temperature):  # pragma: no cover
        raise AssertionError("судью не надо звать, если ответ структурно сломан")

    sample = {"history": "h", "title": "t", "op_answer": "ref"}
    result = await judge_case(sample, "нет секции клиенту", pause_s=0, _call_fn=fake_call)

    assert result["score"] == 0.0
    assert result["hard"].fatal == "no_client_section"
    assert result["judged"] is False


@pytest.mark.asyncio
async def test_judge_case_applies_length_penalty():
    async def fake_call(system, user, *, temperature):
        return _verdict()

    long_client = "Клиенту: " + " ".join(f"слово{i}" for i in range(40))
    sample = {"history": "h", "title": "t", "op_answer": "ref", "action_type": "ASK"}
    result = await judge_case(sample, long_client, repeats=1, pause_s=0, _call_fn=fake_call)

    assert result["score"] == pytest.approx(0.8)  # штраф, а не ноль
    assert "лимите" in result["hard"].detail


# --- holdout ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_holdout_detail_returns_per_case_scores():
    async def fake_generate(history, title, instructions):
        return "Клиенту: перезагрузите кассу"

    async def fake_judge_case(sample, generated, **kw):
        return {"score": 0.8, "axes": {"diagnosis": {"mean": 0.9}}, "invalid": 0}

    samples = [
        {"id": i, "ticket_id": str(i), "title": "t", "history": "h",
         "ai_answer": "a", "op_answer": "Клиенту: перезагрузите кассу",
         "outcome": "corrected"}
        for i in (1, 2, 3)
    ]
    detail = await holdout_detail(
        samples, "инструкция", _generate_fn=fake_generate, _judge_case_fn=fake_judge_case
    )

    assert set(detail["cases"]) == {"1", "2", "3"}
    assert detail["judge_mean"] == pytest.approx(0.8)
    assert detail["axes"]["diagnosis"] == pytest.approx(0.9)
    assert 0.0 <= detail["score"] <= 1.0


@pytest.mark.asyncio
async def test_holdout_score_keeps_float_contract():
    async def fake_generate(history, title, instructions):
        return "Клиенту: ответ"

    async def fake_judge_case(sample, generated, **kw):
        return {"score": 1.0, "axes": {}, "invalid": 0}

    samples = [{"id": 1, "ticket_id": "1", "title": "t", "history": "h",
                "ai_answer": "a", "op_answer": "Клиенту: ответ", "outcome": "accepted"}]
    score = await holdout_score(
        samples, "инструкция", _generate_fn=fake_generate, _judge_case_fn=fake_judge_case
    )
    assert isinstance(score, float) and 0.0 <= score <= 1.0


@pytest.mark.asyncio
async def test_holdout_without_reference_falls_back_to_combined():
    async def fake_generate(history, title, instructions):
        return "Клиенту: ответ"

    async def fake_judge_case(sample, generated, **kw):  # pragma: no cover
        raise AssertionError("без эталона судить не по чему")

    samples = [{"id": 1, "ticket_id": "1", "title": "t", "history": "h",
                "ai_answer": "", "op_answer": None, "outcome": "accepted"}]
    detail = await holdout_detail(
        samples, "инструкция", _generate_fn=fake_generate, _judge_case_fn=fake_judge_case
    )
    assert detail["cases"] == {}
    assert detail["score"] == detail["combined"]
