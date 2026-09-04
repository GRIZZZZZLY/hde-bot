"""Проход планировщика: таймаут на джоб, отчёт в фон, алерт на dead-инбокс.

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


@pytest.fixture(autouse=True)
def reset_report_task():
    scheduler._report_task = None
    scheduler._background_tasks.clear()
    yield
    scheduler._report_task = None
    scheduler._background_tasks.clear()


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


# --- отчёт в фон ------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_report_does_not_block_caller():
    started = asyncio.Event()
    released = asyncio.Event()

    async def slow_report():
        started.set()
        await released.wait()

    assert scheduler._spawn_report("r", slow_report()) is True
    await asyncio.sleep(0)          # даём таску стартовать
    assert started.is_set()
    assert not scheduler._report_task.done()   # вызывающий не ждал отчёт
    released.set()
    await scheduler._report_task


@pytest.mark.asyncio
async def test_second_report_skipped_while_first_alive():
    """Два Playwright на один профиль конкурируют и падают оба — single-flight."""
    released = asyncio.Event()
    second_ran = []

    async def first():
        await released.wait()

    async def second():  # pragma: no cover — не должен запуститься
        second_ran.append(1)

    assert scheduler._spawn_report("first", first()) is True
    await asyncio.sleep(0)
    assert scheduler._spawn_report("second", second()) is False
    released.set()
    await scheduler._report_task
    assert second_ran == []


@pytest.mark.asyncio
async def test_report_allowed_again_after_previous_finished():
    async def quick():
        return None

    assert scheduler._spawn_report("first", quick()) is True
    await scheduler._report_task
    assert scheduler._spawn_report("second", quick()) is True
    await scheduler._report_task


# --- расписание отчётов -----------------------------------------------------

def _only_report_schedule(monkeypatch, hour: int, minute: int = 0) -> list:
    """Проход, в котором работает только ветка расписания отчётов.

    Время — четверг 03.09.2026, MSK. Возвращает список, куда джобы отчёта
    пишут своё имя, когда до них реально дошла очередь.
    """
    import bot.work_schedule as ws
    monkeypatch.setattr(ws, "is_work_day", lambda: True)
    monkeypatch.setattr(ws, "is_work_time", lambda: False)
    monkeypatch.setattr(scheduler.config, "db_backup_dir", "")
    monkeypatch.setattr(
        scheduler, "_now_msk",
        lambda: datetime(2026, 9, 3, hour, minute, tzinfo=scheduler._MSK),
    )

    async def noop(_bot=None):
        return None

    for name in (
        "_maybe_flush_general", "_maybe_send_digest",
        "_maybe_send_daily_value_report", "_maybe_send_reconciliation_digest",
        "_maybe_reconcile_general", "_maybe_send_report_button",
        "_maybe_backfill_dialogue_pairs", "_maybe_reconcile_answers",
        "_maybe_backup_db", "_maybe_alert_dead_inbox",
    ):
        monkeypatch.setattr(scheduler, name, noop)

    ran: list[str] = []

    async def _autorun(_bot):
        ran.append("autorun")

    async def _thursday(_bot):
        ran.append("thursday")

    monkeypatch.setattr(scheduler, "_maybe_auto_run_report", _autorun)
    monkeypatch.setattr(scheduler, "_maybe_thursday_evening_autorun", _thursday)
    return ran


async def _run_pass() -> None:
    await asyncio.wait_for(scheduler.process_scheduled_actions(_FakeBot()), timeout=5)
    if scheduler._report_task is not None:
        await scheduler._report_task


@pytest.mark.asyncio
async def test_thursday_evening_autofill_gets_the_report_slot(monkeypatch):
    """19:00 в четверг — заливка за сегодня, а не отброс single-flight.

    Утренний джоб спавнился каждый проход и забирал единственный слот; таск
    создан, но ещё не выполнялся, поэтому done() у него False и четверговый
    джоб отбрасывался ВСЕГДА. Гейт по времени должен стоять до спавна.
    """
    ran = _only_report_schedule(monkeypatch, 19, 0)
    await _run_pass()
    assert ran == ["thursday"]


@pytest.mark.asyncio
async def test_morning_autorun_still_gets_the_slot_at_nine(monkeypatch):
    ran = _only_report_schedule(monkeypatch, 9, 0)
    await _run_pass()
    assert ran == ["autorun"]


@pytest.mark.asyncio
async def test_no_report_job_spawned_outside_its_window(monkeypatch):
    """Вне окна отчёт не должен занимать слот: проход идёт каждые 30 с."""
    ran = _only_report_schedule(monkeypatch, 15, 0)
    await _run_pass()
    assert ran == []


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
