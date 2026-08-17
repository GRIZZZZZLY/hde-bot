"""Regression tests: the daily report must not be built for a vacation day.

Root cause of the original bug (2026-08-17): vacation mode only gated *today*
(`is_work_day()`), while `last_work_day()` knows the weekly schedule and nothing
about vacation. Vacation expired at 09:00 MSK — exactly the autorun window — so
the autorun asked HDE for Sunday 2026-08-16, a vacation day with zero closed
tickets. HDE omits zero-activity operators from the staff report entirely, and
the parser treats a missing row as fatal:
"Operator 'Игорь Кравцов' not found in staff report".
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from bot import db, scheduler, work_schedule
from bot.reporting import runner

_MSK = work_schedule._MSK


class DummyBot:
    def __init__(self):
        self.sent_messages: list[tuple] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))


@pytest.fixture(autouse=True)
def reset_vacation_state():
    work_schedule.set_vacation(None)
    yield
    work_schedule.set_vacation(None)


@pytest.mark.asyncio
async def test_vacation_window_covers_the_days_off(initialized_db, monkeypatch):
    """/vacation until Mon 09:00 MSK marks Mon..Sun as vacation days, not Monday."""
    monkeypatch.setattr(work_schedule, "_today_msk", lambda: date(2026, 8, 10))
    until = datetime(2026, 8, 17, 9, 0, tzinfo=_MSK).astimezone(timezone.utc)

    await work_schedule.enable_vacation(until)

    assert await work_schedule.is_vacation_day(date(2026, 8, 9)) is False   # before
    assert await work_schedule.is_vacation_day(date(2026, 8, 10)) is True   # first day off
    assert await work_schedule.is_vacation_day(date(2026, 8, 16)) is True   # last day off
    assert await work_schedule.is_vacation_day(date(2026, 8, 17)) is False  # back at work


@pytest.mark.asyncio
async def test_no_vacation_window_means_no_vacation_days(initialized_db):
    assert await work_schedule.is_vacation_day(date(2026, 8, 16)) is False


@pytest.mark.asyncio
async def test_early_workon_ends_the_window_yesterday(initialized_db, monkeypatch):
    """Returning early keeps the days already taken off and frees the rest."""
    monkeypatch.setattr(work_schedule, "_today_msk", lambda: date(2026, 8, 17))
    until = datetime(2026, 8, 20, 9, 0, tzinfo=_MSK).astimezone(timezone.utc)
    await work_schedule.enable_vacation(until)

    monkeypatch.setattr(work_schedule, "_today_msk", lambda: date(2026, 8, 19))
    await work_schedule.disable_vacation()

    assert await work_schedule.is_vacation_day(date(2026, 8, 18)) is True
    assert await work_schedule.is_vacation_day(date(2026, 8, 19)) is False
    assert work_schedule.is_on_vacation() is False


@pytest.mark.asyncio
async def test_vacation_survives_restart(initialized_db, monkeypatch):
    monkeypatch.setattr(work_schedule, "_today_msk", lambda: date(2026, 8, 10))
    until = datetime.now(timezone.utc) + timedelta(days=2)
    await work_schedule.enable_vacation(until)

    work_schedule.set_vacation(None)          # simulate a process restart
    assert work_schedule.is_on_vacation() is False

    await work_schedule.restore_vacation()
    assert work_schedule.is_on_vacation() is True


@pytest.mark.asyncio
async def test_autorun_skips_a_vacation_day(initialized_db, monkeypatch):
    """Mon 09:00 MSK right after vacation: Sunday was off — do not scrape HDE."""
    monkeypatch.setattr(work_schedule, "_today_msk", lambda: date(2026, 8, 10))
    until = datetime(2026, 8, 17, 9, 0, tzinfo=_MSK).astimezone(timezone.utc)
    await work_schedule.enable_vacation(until)

    mon_0900 = datetime(2026, 8, 17, 9, 0, tzinfo=_MSK)
    monkeypatch.setattr(scheduler, "_now_msk", lambda: mon_0900)
    monkeypatch.setattr(work_schedule, "last_work_day", lambda: date(2026, 8, 16))
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    monkeypatch.setattr(scheduler, "_last_report_date", None)

    calls = {"n": 0}

    async def fake_run_report(report_date=None):
        calls["n"] += 1
        return "✅ ok"

    monkeypatch.setattr(runner, "run_report", fake_run_report)

    bot = DummyBot()
    await scheduler._maybe_auto_run_report(bot)

    assert calls["n"] == 0
    assert len(bot.sent_messages) == 1
    assert "отпуск" in bot.sent_messages[0][1].lower()
    assert await db.is_report_sent(date(2026, 8, 16)) is True  # no later nagging


@pytest.mark.asyncio
async def test_autorun_still_runs_for_a_worked_day(initialized_db, monkeypatch):
    """The vacation guard must not block a normal day."""
    mon_0900 = datetime(2026, 8, 17, 9, 0, tzinfo=_MSK)
    monkeypatch.setattr(scheduler, "_now_msk", lambda: mon_0900)
    monkeypatch.setattr(work_schedule, "last_work_day", lambda: date(2026, 8, 16))
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    monkeypatch.setattr(scheduler, "_last_report_date", None)

    calls: list[date] = []

    async def fake_run_report(report_date=None):
        calls.append(report_date)
        return "✅ ok"

    monkeypatch.setattr(runner, "run_report", fake_run_report)

    bot = DummyBot()
    await scheduler._maybe_auto_run_report(bot)

    assert calls == [date(2026, 8, 16)]


@pytest.mark.asyncio
async def test_vacation_command_persists_the_window(initialized_db, monkeypatch):
    """/vacation 3d must record the days off, not only the in-memory flag."""
    from bot.handlers.commands import cmd_vacation

    monkeypatch.setattr(work_schedule, "_today_msk", lambda: date(2026, 8, 10))

    class DummyMessage:
        def __init__(self):
            self.answers: list[str] = []

        async def answer(self, text, **kwargs):
            self.answers.append(text)

    class DummyCommand:
        args = "3d"

    message = DummyMessage()
    await cmd_vacation(message, DummyCommand())

    assert work_schedule.is_on_vacation() is True
    assert await work_schedule.is_vacation_day(date(2026, 8, 10)) is True


@pytest.mark.asyncio
async def test_report_button_skips_a_vacation_day(initialized_db, monkeypatch):
    """No "сформировать отчёт" button for a day that cannot have data."""
    monkeypatch.setattr(work_schedule, "_today_msk", lambda: date(2026, 8, 10))
    until = datetime(2026, 8, 17, 9, 0, tzinfo=_MSK).astimezone(timezone.utc)
    await work_schedule.enable_vacation(until)

    mon_0830 = datetime(2026, 8, 17, 8, 30, tzinfo=_MSK)
    monkeypatch.setattr(scheduler, "_now_msk", lambda: mon_0830)
    monkeypatch.setattr(work_schedule, "last_work_day", lambda: date(2026, 8, 16))
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    monkeypatch.setattr(scheduler, "_report_button_sent", None)
    monkeypatch.setattr(scheduler, "_last_report_date", None)

    bot = DummyBot()
    await scheduler._maybe_send_report_button(bot)

    assert bot.sent_messages == []
