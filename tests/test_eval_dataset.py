"""Датасет офлайн-оценки промптов: источник — ночная сверка, корзина — тикет.

Кнопочный сигнал (optimization_samples) встал 2026-06-21, живой эталон даёт
reconcile. Здесь проверяется, что загрузчик отдаёт ту же форму строки, что
старый источник (evaluator/judge/eval_prompt читают одни ключи), берёт только
отсуженные пары, и что сплит не разводит предложения одного тикета по train и
holdout.
"""
import aiosqlite
import pytest

from bot import db as _db
from bot.optimizer.dataset import (
    exclude_tickets,
    group_key,
    load_golden_ticket_ids,
    split_samples,
)

_EVALUATOR_KEYS = {
    "id", "ticket_id", "title", "history", "ai_answer", "op_answer",
    "outcome", "confidence",
}


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


async def _suggestion(
    ticket_id: str, *, trigger: str = "first", label: str | None = "corrected",
    reference: str = "Проверьте кабель ККТ и переподключите по USB",
    history: str = "Клиент: касса не печатает чек", title: str = "Не печатает чек",
) -> int:
    sid = await _db.record_suggestion(
        ticket_id=ticket_id, topic_id=1, trigger_source=trigger,
        context_until_post_id="10", pipeline_version="v1", prompt_version="p1",
        title=title, history=history, ai_answer="Клиенту: перезагрузите кассу",
        confidence=70,
    )
    if label is not None:
        await _db.set_judge_result(
            sid, reference_answer=reference, label=label,
            detail=f"judge:bot_wrong_fact {label}", category="bot_wrong_fact",
        )
    return sid


# --- загрузчик ---

@pytest.mark.asyncio
async def test_evaluation_sample_has_optimization_sample_shape():
    await _db.init_db()
    await _suggestion("T100")

    samples = await _db.get_evaluation_samples(days=30)

    assert len(samples) == 1
    row = samples[0]
    assert _EVALUATOR_KEYS <= set(row)
    assert row["ticket_id"] == "T100"
    assert row["op_answer"] == "Проверьте кабель ККТ и переподключите по USB"
    assert row["outcome"] == "corrected"
    assert row["history"] == "Клиент: касса не печатает чек"


@pytest.mark.asyncio
async def test_evaluation_samples_skip_unjudged():
    await _db.init_db()
    await _suggestion("T101", label=None)

    assert await _db.get_evaluation_samples(days=30) == []


@pytest.mark.asyncio
async def test_evaluation_samples_skip_empty_reference():
    await _db.init_db()
    await _suggestion("T102", reference="")

    assert await _db.get_evaluation_samples(days=30) == []


@pytest.mark.asyncio
async def test_evaluation_samples_skip_empty_history():
    await _db.init_db()
    await _suggestion("T103", history="")

    assert await _db.get_evaluation_samples(days=30) == []


@pytest.mark.asyncio
async def test_human_label_wins_over_judge_label():
    """Оператор видел тикет — его вердикт сильнее вердикта модели."""
    await _db.init_db()
    sid = await _suggestion("T104", label="corrected")
    await _db.record_suggestion_event(sid, "approved")

    samples = await _db.get_evaluation_samples(days=30)

    assert samples[0]["outcome"] == "accepted"


@pytest.mark.asyncio
async def test_evaluation_samples_respect_days_window():
    await _db.init_db()
    sid = await _suggestion("T105")
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        await conn.execute(
            "UPDATE ai_suggestions SET created_at=datetime('now', '-120 days') WHERE id=?",
            (sid,),
        )
        await conn.commit()

    assert await _db.get_evaluation_samples(days=90) == []
    assert len(await _db.get_evaluation_samples(days=180)) == 1


@pytest.mark.asyncio
async def test_evaluation_samples_limit():
    await _db.init_db()
    for i in range(5):
        await _suggestion(f"T20{i}")

    assert len(await _db.get_evaluation_samples(days=30, limit=2)) == 2


# --- сплит по тикету ---

def test_group_key_prefers_ticket_id():
    assert group_key({"id": 7, "ticket_id": "T1"}) == "T1"
    assert group_key({"id": 7}) == "7"
    assert group_key({"id": 7, "ticket_id": ""}) == "7"
    assert group_key({}) == ""


def test_split_keeps_all_samples_of_one_ticket_together():
    """Три предложения одного тикета (first / button / reply) — одна корзина."""
    samples = [
        {"id": 1, "ticket_id": "T1"},
        {"id": 2, "ticket_id": "T1"},
        {"id": 3, "ticket_id": "T1"},
    ]
    train, holdout = split_samples(samples)
    assert len(train) in (0, 3) and len(holdout) in (0, 3)


def test_split_never_shares_a_ticket_between_train_and_holdout():
    samples = [
        {"id": i, "ticket_id": f"T{i % 120}"}
        for i in range(600)
    ]
    train, holdout = split_samples(samples)
    train_tickets = {s["ticket_id"] for s in train}
    holdout_tickets = {s["ticket_id"] for s in holdout}
    assert not (train_tickets & holdout_tickets)
    assert len(train) + len(holdout) == 600


def test_split_falls_back_to_sample_id_without_ticket():
    samples = [{"id": i} for i in range(1000)]
    train, holdout = split_samples(samples)
    assert 0.12 <= len(holdout) / 1000 <= 0.28
    assert len(train) + len(holdout) == 1000


# --- независимость golden set ---

def test_exclude_tickets_drops_golden_samples():
    samples = [{"ticket_id": "T1"}, {"ticket_id": "T2"}, {"ticket_id": "T3"}]
    kept = exclude_tickets(samples, {"T2"})
    assert [s["ticket_id"] for s in kept] == ["T1", "T3"]


def test_exclude_tickets_without_ids_is_identity():
    samples = [{"ticket_id": "T1"}]
    assert exclude_tickets(samples, set()) == samples


def test_load_golden_ticket_ids_reads_file(tmp_path):
    path = tmp_path / "golden_tickets.txt"
    path.write_text("11111\n22222\n\n", encoding="utf-8")
    assert load_golden_ticket_ids(str(path)) == {"11111", "22222"}


def test_load_golden_ticket_ids_missing_file_is_empty(tmp_path):
    """Golden на этой машине не собирали — исключать нечего, это не ошибка."""
    assert load_golden_ticket_ids(str(tmp_path / "nope.txt")) == set()
