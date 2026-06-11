"""Regression tests for daily-report duplicate rows.

Root cause of the original bug: the dedup marker (``report_runs``) was written
*after* the slow ``run_report`` scrape+append, so two triggers in the 19:00
window (two bot instances, or a restart mid-run) both saw "not sent" and both
appended a row. Fix: claim the date atomically *before* running the report.
"""
from datetime import date, datetime

import pytest

from bot import db, scheduler
from bot.reporting import runner


class DummyBot:
    def __init__(self):
        self.sent_messages: list[tuple] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))


@pytest.mark.asyncio
async def test_mark_report_sent_claims_once(initialized_db):
    d = date(2026, 6, 11)
    assert await db.mark_report_sent(d) is True   # first caller claims the date
    assert await db.mark_report_sent(d) is False  # second caller is rejected
    assert await db.is_report_sent(d) is True


@pytest.mark.asyncio
async def test_clear_report_sent_releases_claim(initialized_db):
    d = date(2026, 6, 11)
    assert await db.mark_report_sent(d) is True
    await db.clear_report_sent(d)
    assert await db.is_report_sent(d) is False
    assert await db.mark_report_sent(d) is True   # re-claim possible after release


@pytest.mark.asyncio
async def test_thursday_autorun_no_double_write_on_retrigger(initialized_db, monkeypatch):
    """A second trigger arriving while the first scrape is in flight (second
    bot instance / restart within the 19:00 window) must NOT append again."""
    thu_1900 = datetime(2026, 6, 11, 19, 0, tzinfo=scheduler._MSK)
    monkeypatch.setattr(scheduler, "_now_msk", lambda: thu_1900)
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    monkeypatch.setattr(scheduler, "_thursday_evening_done", None)
    monkeypatch.setattr(scheduler, "_last_report_date", None)

    bot = DummyBot()
    calls = {"n": 0}
    claimed_at_run: list[bool] = []

    async def fake_run_report(report_date=None):
        calls["n"] += 1
        # The row must already be claimed before the scrape starts.
        claimed_at_run.append(await db.is_report_sent(date(2026, 6, 11)))
        if calls["n"] == 1:
            # Simulate a concurrent trigger that lost the in-memory guard.
            scheduler._thursday_evening_done = None
            await scheduler._maybe_thursday_evening_autorun(bot)
        return "✅ ok"

    monkeypatch.setattr(runner, "run_report", fake_run_report)

    await scheduler._maybe_thursday_evening_autorun(bot)

    assert calls["n"] == 1
    assert claimed_at_run == [True]  # claimed before run, not after


@pytest.mark.asyncio
async def test_thursday_autorun_releases_claim_on_failure(initialized_db, monkeypatch):
    """If the scrape fails, the claim is released so a later pass can retry."""
    thu_1900 = datetime(2026, 6, 11, 19, 0, tzinfo=scheduler._MSK)
    monkeypatch.setattr(scheduler, "_now_msk", lambda: thu_1900)
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    monkeypatch.setattr(scheduler, "_thursday_evening_done", None)
    monkeypatch.setattr(scheduler, "_last_report_date", None)

    bot = DummyBot()

    async def failing_run_report(report_date=None):
        raise RuntimeError("HDE scrape failed")

    monkeypatch.setattr(runner, "run_report", failing_run_report)

    await scheduler._maybe_thursday_evening_autorun(bot)

    assert await db.is_report_sent(date(2026, 6, 11)) is False
