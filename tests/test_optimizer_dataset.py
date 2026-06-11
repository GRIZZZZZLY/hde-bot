"""Tests for deterministic train/holdout split."""
import pytest

from bot.optimizer.dataset import HOLDOUT_RATIO, is_holdout, split_samples
from bot.optimizer.evaluator import combined_score


def test_is_holdout_deterministic():
    assert is_holdout(42) == is_holdout(42)
    assert is_holdout("42") == is_holdout(42)  # str/int одинаково


def test_split_ratio_approx():
    samples = [{"id": i} for i in range(1000)]
    train, holdout = split_samples(samples)
    assert 0.12 <= len(holdout) / 1000 <= 0.28  # ~20% с допуском
    assert len(train) + len(holdout) == 1000


def test_split_stable_across_calls():
    samples = [{"id": i} for i in range(100)]
    _, h1 = split_samples(samples)
    _, h2 = split_samples(samples)
    assert [s["id"] for s in h1] == [s["id"] for s in h2]


def test_split_empty():
    assert split_samples([]) == ([], [])


@pytest.mark.asyncio
async def test_combined_score_max_samples_none_evaluates_all():
    calls = []

    async def fake_generate(history, title, instructions):
        calls.append(history)
        return "Клиенту: перезагрузите кассу"

    samples = [
        {"id": i, "ticket_id": str(i), "title": "t", "history": f"h{i}",
         "ai_answer": "a", "op_answer": "Клиенту: перезагрузите кассу",
         "outcome": "corrected"}
        for i in range(25)
    ]
    await combined_score(samples, "инструкция", max_samples=None, _generate_fn=fake_generate)
    assert len(calls) == 25  # не обрезано до дефолтных 20
