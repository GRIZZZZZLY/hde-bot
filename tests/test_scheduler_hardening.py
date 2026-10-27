"""Проход планировщика: таймаут на джоб, отчёт только вручную, алерт на dead-инбокс.

Что здесь защищается по существу: один зависший await не должен останавливать
весь проход. Watchdog от этого не спасает — он отдельный таск и продолжает
пинговать systemd, поэтому процесс выглядит здоровым, пока pre-SLA молчат.
"""
import asyncio
from datetime import datetime

import pytest

import bot.db as db_module
from bot import scheduler
from bot.config import config


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text=None, **kw):
        self.sent.append(text if text is not None else kw.get("text", ""))


# --- таймаут джоба ----------------------------------------------------------

@pytest.mark.asyncio
async def test_run_job_returns_false_on_timeout_without_raising():
    async def hangs():
        await asyncio.sleep(10)

    assert await scheduler._run_job("hang", hangs(), timeout=0.05) is False


@pytest.mark.asyncio
async def test_run_job_cancels_the_hung_coroutine():
    """Зависший джоб должен быть снят, а не продолжать жить в фоне."""
    cancelled = asyncio.Event()

    async def hangs():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    await scheduler._run_job("hang", hangs(), timeout=0.05)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_run_job_swallows_exception():
    async def boom():
        raise RuntimeError("HDE 500")

    assert await scheduler._run_job("boom", boom(), timeout=1) is False


@pytest.mark.asyncio
async def test_run_job_returns_true_on_success():
    async def ok():
        return None

    assert await scheduler._run_job("ok", ok(), timeout=1) is True


@pytest.mark.asyncio
async def test_hung_job_does_not_block_the_rest_of_the_pass(monkeypatch):
    """Главный инвариант: повисший джоб не съедает проход целиком."""
    monkeypatch.setattr(scheduler, "_JOB_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(scheduler, "_REPORT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(scheduler, "_NIGHTLY_BATCH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(scheduler.config, "db_backup_dir", "")
    # не рабочий день и не рабочее время: остаётся короткий хвост прохода
    import bot.work_schedule as ws
    monkeypatch.setattr(ws, "is_work_day", lambda: False)
    monkeypatch.setattr(ws, "is_work_time", lambda: False)

    async def hangs(_bot):
        await asyncio.sleep(10)

    reached = []

    async def marker(_bot):
        reached.append("dead_alert")

    monkeypatch.setattr(scheduler, "_maybe_reconcile_general", hangs)
    monkeypatch.setattr(scheduler, "_maybe_backfill_dialogue_pairs", hangs)
    monkeypatch.setattr(scheduler, "_maybe_reconcile_answers", hangs)
    monkeypatch.setattr(scheduler, "_maybe_alert_dead_inbox", marker)

    await asyncio.wait_for(scheduler.process_scheduled_actions(_FakeBot()), timeout=5)

    assert reached == ["dead_alert"]  # хвост прохода отработал


# --- отчёт -----------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("day", [3, 6])  # чт 03.09.2026 и вс 06.09.2026
async def test_scheduler_sends_nothing_about_the_report(monkeypatch, day):
    """Ни кнопки-напоминания в 8:30 Пн–Чт, ни воскресной сводки: отчёт только вручную."""
    import bot.reporting.runner as runner
    import bot.work_schedule as ws
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    monkeypatch.setattr(ws, "is_work_day", lambda: True)
    monkeypatch.setattr(ws, "is_work_time", lambda: False)
    monkeypatch.setattr(scheduler.config, "db_backup_dir", "")
    monkeypatch.setattr(
        scheduler, "_now_msk",
        lambda: datetime(2026, 9, day, 8, 30, tzinfo=scheduler._MSK),
    )

    async def noop(_bot=None):
        return None

    for name in (
        "_maybe_flush_general", "_maybe_send_digest",
        "_maybe_send_daily_value_report", "_maybe_send_reconciliation_digest",
        "_maybe_reconcile_general", "_maybe_backfill_dialogue_pairs",
        "_maybe_reconcile_answers", "_maybe_backup_db", "_maybe_alert_dead_inbox",
    ):
        monkeypatch.setattr(scheduler, name, noop)

    bot = _FakeBot()
    await asyncio.wait_for(scheduler.process_scheduled_actions(bot), timeout=5)
    assert bot.sent == []


# --- алерт на dead-инбокс ---------------------------------------------------

async def _mark_dead(event_id: str) -> None:
    """Кладём событие сразу в 'dead'.

    Проводить его через попытки нельзя: между ними backoff в десятки секунд, а
    сам конечный автомат инбокса проверяется в test_inbox.py — здесь нужен
    только факт непустого 'dead'.
    """
    await db_module.enqueue_event(event_id, "{}")
    async with db_module.connect() as conn:
        await conn.execute(
            "UPDATE webhook_inbox SET status='dead' WHERE event_id=?", (event_id,)
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_dead_alert_fires_once_then_stays_quiet():
    await db_module.init_db()
    await _mark_dead("E1")
    bot = _FakeBot()

    await scheduler._maybe_alert_dead_inbox(bot)
    await scheduler._maybe_alert_dead_inbox(bot)

    assert len(bot.sent) == 1
    assert "dead" in bot.sent[0]


@pytest.mark.asyncio
async def test_dead_alert_fires_again_when_count_grows():
    await db_module.init_db()
    await _mark_dead("E1")
    bot = _FakeBot()
    await scheduler._maybe_alert_dead_inbox(bot)

    await _mark_dead("E2")
    await scheduler._maybe_alert_dead_inbox(bot)

    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_dead_alert_silent_when_queue_clean():
    await db_module.init_db()
    bot = _FakeBot()
    await scheduler._maybe_alert_dead_inbox(bot)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_alert_threshold_drops_after_cleanup():
    """Очередь почистили — планка опускается, иначе следующий сбой промолчит."""
    await db_module.init_db()
    await _mark_dead("E1")
    bot = _FakeBot()
    await scheduler._maybe_alert_dead_inbox(bot)
    assert len(bot.sent) == 1

    async with db_module.connect() as conn:
        await conn.execute("DELETE FROM webhook_inbox")
        await conn.commit()
    await scheduler._maybe_alert_dead_inbox(bot)   # опускает планку до нуля
    assert await db_module.get_setting(scheduler._INBOX_DEAD_ALERT_KEY, "-1") == "0"

    await _mark_dead("E2")
    await scheduler._maybe_alert_dead_inbox(bot)
    assert len(bot.sent) == 2                      # новый сбой снова слышен


@pytest.mark.asyncio
async def test_dead_alert_survives_telegram_failure():
    await db_module.init_db()
    await _mark_dead("E1")

    class _BrokenBot:
        async def send_message(self, *a, **kw):
            raise RuntimeError("telegram down")

    await scheduler._maybe_alert_dead_inbox(_BrokenBot())  # не роняет проход


@pytest.mark.asyncio
async def test_timers_run_while_the_main_pass_is_still_busy(monkeypatch):
    """Pre-SLA has its own loop: a slow pass (reconcile, drafts, Telegram) must not
    delay it. Before 2026-10-12 timers sat at the end of the pass and a 7-minute SLA
    warning came 80 s late."""
    monkeypatch.setattr(scheduler, "_TIMERS_INTERVAL_SECONDS", 0.01)
    ticks = []

    async def slow_pass(_bot):
        await asyncio.sleep(10)

    async def timers(_bot):
        ticks.append(1)

    monkeypatch.setattr(scheduler, "process_scheduled_actions", slow_pass)
    monkeypatch.setattr(scheduler, "process_due_timers", timers)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_scheduler(_FakeBot(), stop))
    await asyncio.sleep(0.2)
    stop.set()
    task.cancel()

    assert len(ticks) >= 3
