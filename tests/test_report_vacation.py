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

from bot import work_schedule

_MSK = work_schedule._MSK


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
