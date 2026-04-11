"""Smoke test: feedback pending state lifecycle."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from bot.db import init_db, save_ai_feedback_pending, get_ai_feedback_pending, delete_ai_feedback_pending


@pytest.fixture(autouse=True)
def use_test_db(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.db.DB_PATH", str(tmp_path / "test.db"))


@pytest.mark.asyncio
async def test_feedback_pending_lifecycle():
    await init_db()
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    await save_ai_feedback_pending(
        topic_id=42,
        ticket_id="123",
        history="Клиент: помогите\nСотрудник: ок",
        title="Проблема с принтером",
        expires_at=expires,
    )

    pending = await get_ai_feedback_pending(42)
    assert pending is not None
    assert pending["ticket_id"] == "123"
    assert pending["title"] == "Проблема с принтером"

    await delete_ai_feedback_pending(42)
    assert await get_ai_feedback_pending(42) is None


@pytest.mark.asyncio
async def test_feedback_pending_expired():
    await init_db()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    await save_ai_feedback_pending(
        topic_id=99,
        ticket_id="456",
        history="...",
        title="...",
        expires_at=past,
    )

    pending = await get_ai_feedback_pending(99)
    assert pending is None  # expired → auto-deleted
