"""Tests for LLM-judge."""
import json

import pytest

from bot.optimizer.judge import build_judge_prompt, holdout_score, judge_answer

_GOOD_VERDICT = json.dumps({
    "diagnosis_correct": True, "equipment_named": True, "format_ok": True,
    "length_ok": True, "sounds_human": False, "overall": 6,
    "reason": "шаблонная вежливость в конце",
})


def test_build_judge_prompt_contains_inputs_and_style_guide():
    system, user = build_judge_prompt(
        history="Касса Атол 30Ф не печатает чек",
        title="Не печатает",
        candidate="Клиенту: перезагрузите кассу",
        op_answer="Перезагрузите кассу, проверьте ленту",
    )
    assert "Атол 30Ф" in user
    assert "перезагрузите кассу" in user.lower()
    assert "Памятка" in system or "Клиенту" in system  # формат описан
    assert "канцелярит" in system.lower() or "Запрещено" in system  # стайлгайд вшит


@pytest.mark.asyncio
async def test_judge_answer_parses_json():
    async def fake_call(system, user):
        return _GOOD_VERDICT

    verdict = await judge_answer("h", "t", "cand", "ref", _call_fn=fake_call)
    assert verdict["overall"] == 6
    assert verdict["sounds_human"] is False


@pytest.mark.asyncio
async def test_judge_answer_retries_once_then_none():
    calls = []

    async def bad_call(system, user):
        calls.append(1)
        return "это не json"

    verdict = await judge_answer("h", "t", "cand", "ref", _call_fn=bad_call)
    assert verdict is None
    assert len(calls) == 2  # один повтор


@pytest.mark.asyncio
async def test_holdout_score_formula():
    async def fake_generate(history, title, instructions):
        return "Клиенту: перезагрузите кассу"

    async def fake_judge(history, title, candidate, op_answer):
        return json.loads(_GOOD_VERDICT)  # overall=6 → 0.6

    samples = [
        {"id": 1, "ticket_id": "1", "title": "t", "history": "h",
         "ai_answer": "a", "op_answer": "Клиенту: перезагрузите кассу",
         "outcome": "corrected"},
    ]
    score = await holdout_score(
        samples, "инструкция", _generate_fn=fake_generate, _judge_fn=fake_judge
    )
    # combined_score: 0.7*(1.0*0.4) + 0.3*1.0 = 0.58; judge 0.6 → 0.5*0.58+0.5*0.6 = 0.59
    assert 0.55 <= score <= 0.63


@pytest.mark.asyncio
async def test_holdout_score_without_op_answers_falls_back_to_base():
    async def fake_generate(history, title, instructions):
        return "Клиенту: ответ"

    async def fake_judge(history, title, candidate, op_answer):
        raise AssertionError("judge не должен вызываться без op_answer")

    samples = [
        {"id": 1, "ticket_id": "1", "title": "t", "history": "h",
         "ai_answer": "", "op_answer": None, "outcome": "accepted"},
    ]
    score = await holdout_score(
        samples, "инструкция", _generate_fn=fake_generate, _judge_fn=fake_judge
    )
    assert 0.0 <= score <= 1.0
