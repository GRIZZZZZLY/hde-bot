"""Regression tests for daily-report duplicate rows.

Root cause of the original bug: the dedup marker (``report_runs``) was written
*after* the slow ``run_report`` scrape+append, so two triggers in the 19:00
window (two bot instances, or a restart mid-run) both saw "not sent" and both
appended a row. Fix: claim the date atomically *before* running the report.
"""
from datetime import date

import pytest

from bot import db


@pytest.mark.asyncio
async def test_mark_report_sent_claims_once(initialized_db):
    d = date(2026, 6, 11)
    assert await db.mark_report_sent(d) is True   # first caller claims the date
    assert await db.mark_report_sent(d) is False  # second caller is rejected
    assert await db.is_report_sent(d) is True
