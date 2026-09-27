"""Tests for the durable webhook inbox (ADR 2026-07-12).

Инварианты: 200 OK = событие durable (pending); обработано только после
'completed'; краш процесса (истёкший lease у 'processing') → повторная выборка;
retry/backoff; 'dead' после лимита; повторная доставка после 'completed' не
выполняет работу второй раз; единственная система дедупа приёма.
"""
import datetime as _dt

import bot.db as db_module
from bot.db.inbox import (
    claim_next_event,
    count_inbox_by_status,
    enqueue_event,
    get_inbox_event,
    mark_completed,
    mark_failed,
)

_T0 = _dt.datetime(2026, 7, 12, 10, 0, 0, tzinfo=_dt.timezone.utc)


def _later(seconds: int) -> _dt.datetime:
    return _T0 + _dt.timedelta(seconds=seconds)


async def test_enqueue_is_idempotent_by_event_id():
    await db_module.init_db()
    assert await enqueue_event("E1", '{"x":1}', now=_T0) is True
    assert await enqueue_event("E1", '{"x":1}', now=_T0) is False   # дубль доставки
    assert (await count_inbox_by_status()).get("pending") == 1


async def test_claim_marks_processing_and_leases():
    await db_module.init_db()
    await enqueue_event("E1", "p", now=_T0)
    row = await claim_next_event(lease_seconds=120, now=_T0)
    assert row["event_id"] == "E1" and row["attempts"] == 1
    ev = await get_inbox_event("E1")
    assert ev["status"] == "processing"
    # пока lease держится — событие не выдаётся повторно
    assert await claim_next_event(lease_seconds=120, now=_later(10)) is None


async def test_completed_is_terminal_and_redelivery_does_no_work():
    await db_module.init_db()
    await enqueue_event("E1", "p", now=_T0)
    await claim_next_event(now=_T0)
    await mark_completed("E1")
    assert (await get_inbox_event("E1"))["status"] == "completed"
    assert await claim_next_event(now=_later(5)) is None            # не переобрабатывается
    # повторная доставка того же event_id не создаёт новую работу
    assert await enqueue_event("E1", "p", now=_later(5)) is False
    assert await claim_next_event(now=_later(6)) is None


async def test_crash_leaves_processing_then_expired_lease_is_reclaimed():
    """SIGKILL-сценарий: воркер забрал событие (processing), процесс умер до
    completed/failed. После рестарта истёкший lease → событие снова выбирается."""
    await db_module.init_db()
    await enqueue_event("E1", "p", now=_T0)
    first = await claim_next_event(lease_seconds=120, now=_T0)
    assert first["attempts"] == 1
    # процесс "умер" — ни completed, ни failed; строка осталась в processing
    # до истечения lease повторно не выдаётся
    assert await claim_next_event(now=_later(60)) is None
    # lease истёк (120с) → рестартовавший воркер переклеймит, attempts растёт
    reclaimed = await claim_next_event(lease_seconds=120, now=_later(200))
    assert reclaimed["event_id"] == "E1" and reclaimed["attempts"] == 2


async def test_failed_schedules_backoff_then_becomes_claimable():
    await db_module.init_db()
    await enqueue_event("E1", "p", now=_T0)
    await claim_next_event(now=_T0)
    await mark_failed("E1", "boom", backoff_seconds=30, now=_T0)
    assert (await get_inbox_event("E1"))["status"] == "pending"
    # до next_attempt_at не выдаётся
    assert await claim_next_event(now=_later(10)) is None
    # после backoff — снова выбирается
    row = await claim_next_event(now=_later(31))
    assert row["event_id"] == "E1" and row["attempts"] == 2


async def test_dead_after_max_attempts_and_never_claimed_again():
    await db_module.init_db()
    await enqueue_event("E1", "p", now=_T0)
    # 5 неудачных попыток (max_attempts=5): claim инкрементит attempts, fail откладывает
    for i in range(5):
        row = await claim_next_event(max_attempts=5, now=_later(i * 100))
        assert row is not None
        await mark_failed("E1", f"fail{i}", backoff_seconds=1, now=_later(i * 100))
    # attempts исчерпаны → следующий claim переводит в dead и ничего не выдаёт
    assert await claim_next_event(max_attempts=5, now=_later(1000)) is None
    assert (await get_inbox_event("E1"))["status"] == "dead"


async def test_claim_skips_tickets_already_in_flight():
    """Тикет в работе: его события (и его собственная строка с истёкшим lease)
    не выдаются, пока хендлер не закончил; другие тикеты — выдаются."""
    await db_module.init_db()
    await enqueue_event("E1", '{"ticket_id": "T1"}', now=_T0)
    await enqueue_event("E2", '{"ticket_id": "T1"}', now=_T0)
    await enqueue_event("E3", '{"ticket_id": "T2"}', now=_T0)
    await enqueue_event("E4", "not json", now=_T0)
    first = await claim_next_event(lease_seconds=120, now=_T0)
    assert first["event_id"] == "E1"
    # lease E1 истёк, но T1 ещё обрабатывается → ни E1, ни E2
    row = await claim_next_event(now=_later(200), busy_tickets=frozenset({"T1"}))
    assert row["event_id"] == "E3"
    row = await claim_next_event(now=_later(200), busy_tickets=frozenset({"T1", "T2"}))
    assert row["event_id"] == "E4"                    # битый payload не блокирует очередь
    assert await claim_next_event(now=_later(200), busy_tickets=frozenset({"T1", "T2", ""})) is None
