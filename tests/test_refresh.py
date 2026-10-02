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
