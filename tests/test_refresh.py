from __future__ import annotations

import pytest

import bot.db as db_module
from bot.config import config
from bot.refresh import refresh_topics


class DummyBot:
    def __init__(self):
        self.deleted_topics: list[int] = []
        self.edited_topics: list[int] = []

    async def edit_forum_topic(self, chat_id, message_thread_id, **kwargs):
        self.edited_topics.append(message_thread_id)

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self.deleted_topics.append(message_thread_id)

    async def create_forum_topic(self, chat_id, name, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(message_thread_id=9999)

    async def send_message(self, *args, **kwargs):
        pass

    async def pin_message(self, *args, **kwargs):
        pass


def make_topic(**kwargs):
    defaults = dict(
        ticket_id="1",
        unique_id="TST-1",
        ticket_name="Test ticket",
        company_name="ACME",
        topic_id=100,
        topic_state="active",
        priority="medium",
        status="open",
        owner_id="",
        owner_name="",
        delete_after_at=None,
        last_client_reply_at=None,
        last_staff_reply_at=None,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
        reassurance_sent_at=None,
        hde_link="",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        deleted_at=None,
        last_assigned_at=None,
        ai_summary_sent_at=None,
        chat_id=config.group_chat_id,
    )
    defaults.update(kwargs)
    return db_module.TicketTopic(**defaults)


async def _noop_update_topic(ticket_id, **kwargs):
    pass


@pytest.mark.asyncio
async def test_refresh_skips_deletion_when_api_error(monkeypatch):
    """API error (None) → topic not deleted (fail-safe)."""
    topic = make_topic(ticket_id="1", topic_id=100)

    monkeypatch.setattr("bot.db.list_active_topics", lambda: _async([topic]))
    monkeypatch.setattr("bot.db.list_topics_by_state", lambda state: _async([]))
    monkeypatch.setattr("bot.db.count_active_topics", lambda: _async(1))
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", _async_method([]))
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_ticket_open_status", _async_method(None))
    monkeypatch.setattr("bot.db.update_topic", _noop_update_topic)

    bot = DummyBot()
    result = await refresh_topics(bot)

    assert 100 not in bot.deleted_topics
    assert result.deleted == []


@pytest.mark.asyncio
async def test_refresh_skips_deletion_when_ticket_still_open(monkeypatch):
    """is_deletable=False (ticket open/pending) → topic not deleted."""
    topic = make_topic(ticket_id="2", topic_id=200)

    monkeypatch.setattr("bot.db.list_active_topics", lambda: _async([topic]))
    monkeypatch.setattr("bot.db.list_topics_by_state", lambda state: _async([]))
    monkeypatch.setattr("bot.db.count_active_topics", lambda: _async(1))
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", _async_method([]))
    monkeypatch.setattr(
        "bot.hde_api.HDEApiClient.get_ticket_open_status",
        _async_method((False, "https://hde.example.com/t/2")),
    )
    monkeypatch.setattr("bot.db.update_topic", _noop_update_topic)

    bot = DummyBot()
    result = await refresh_topics(bot)

    assert 200 not in bot.deleted_topics
    assert result.deleted == []


@pytest.mark.asyncio
async def test_refresh_deletes_when_ticket_closed(monkeypatch):
    """is_deletable=True (resolved/closed) → topic deleted + included in result."""
    topic = make_topic(ticket_id="3", topic_id=300, ticket_name="Done ticket")

    monkeypatch.setattr("bot.db.list_active_topics", lambda: _async([topic]))
    monkeypatch.setattr("bot.db.list_topics_by_state", lambda state: _async([]))
    monkeypatch.setattr("bot.db.count_active_topics", lambda: _async(0))
    monkeypatch.setattr("bot.db.update_topic", _noop_update_topic)
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", _async_method([]))
    monkeypatch.setattr(
        "bot.hde_api.HDEApiClient.get_ticket_open_status",
        _async_method((True, "https://hde.example.com/t/3")),
    )

    bot = DummyBot()
    result = await refresh_topics(bot)

    assert 300 in bot.deleted_topics
    assert len(result.deleted) == 1
    name, ticket_id, link = result.deleted[0]
    assert name == "Done ticket"
    assert ticket_id == "3"
    assert link == "https://hde.example.com/t/3"


# ── helpers ──────────────────────────────────────────────────────────────────

async def _async(value):
    return value


def _async_method(value):
    async def method(self, *args, **kwargs):
        return value
    return method


@pytest.mark.asyncio
async def test_orphan_purge_only_touches_recently_deleted_topics(initialized_db, monkeypatch):
    """Step 4 re-deletes Telegram topics only for records deleted in the last day:
    re-sweeping the whole history cost ~2000 Telegram calls per refresh."""
    from datetime import timedelta
    from bot.time_utils import to_storage, utcnow
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", _async_method([]))
    await db_module.upsert_topic("OLD", 300, chat_id=config.group_chat_id, topic_state="deleted",
                                 deleted_at=to_storage(utcnow() - timedelta(days=40)))
    await db_module.upsert_topic("NEW", 301, chat_id=config.group_chat_id, topic_state="deleted",
                                 deleted_at=to_storage(utcnow() - timedelta(hours=2)))
    bot = DummyBot()

    await refresh_topics(bot)

    assert bot.deleted_topics == [301]


@pytest.mark.asyncio
async def test_auto_refresh_runs_every_30_minutes_only_in_working_hours(monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone
    import bot.scheduler as scheduler
    import bot.work_schedule as work_schedule
    calls = []

    async def fake_refresh(bot):
        calls.append(bot)
        from bot.refresh import RefreshResult
        return RefreshResult(active_before=0, hde_count=0)

    monkeypatch.setattr("bot.refresh.refresh_topics", fake_refresh)
    monkeypatch.setattr(scheduler, "_last_auto_refresh_at", None)
    monkeypatch.setattr(work_schedule, "anyone_at_work", lambda: False)
    await scheduler._maybe_auto_refresh("bot")
    assert scheduler._auto_refresh_task is None  # nobody at work → no run

    monkeypatch.setattr(work_schedule, "anyone_at_work", lambda: True)
    await scheduler._maybe_auto_refresh("bot")
    await scheduler._auto_refresh_task
    await scheduler._maybe_auto_refresh("bot")  # 0 min later → skipped
    assert calls == ["bot"]

    monkeypatch.setattr(scheduler, "_last_auto_refresh_at", datetime.now(timezone.utc) - timedelta(minutes=31))
    await scheduler._maybe_auto_refresh("bot")
    await scheduler._auto_refresh_task
    assert calls == ["bot", "bot"]
