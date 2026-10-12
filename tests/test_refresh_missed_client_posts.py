"""Refresh replays client posts that HDE delivered as staff_reply (rules 55/59, 2026-10-12)."""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest

import bot.refresh as refresh
from bot.formatter import _parse_hde_post_date
from bot.hde_api import HDEPost, HDETicket
from bot.time_utils import to_storage, utcnow
from tests.test_refresh import make_topic

_MSK = timezone(timedelta(hours=3))
CLIENT, STAFF = 5001, 6001


def _hde_time(minutes_ago: float) -> str:
    return (utcnow() - timedelta(minutes=minutes_ago)).astimezone(_MSK).strftime("%H:%M:%S %d.%m.%Y")


def _post(pid: int, user_id: int, minutes_ago: float, text: str = "") -> HDEPost:
    return HDEPost(post_id=pid, user_id=user_id, text=text or f"post {pid}",
                   date_created=_hde_time(minutes_ago), user_name="Виктория", files=[])


class FakeClient:
    def __init__(self, posts, groups):
        self.posts, self.groups = posts, groups

    async def get_ticket_posts(self, ticket_id, limit=20, page=None):
        return self.posts  # oldest first, like HDEApiClient

    async def get_user_group_type(self, uid):
        return self.groups.get(uid)


TICKET = HDETicket(ticket_id="213746", unique_id="213746", title="Эвотор", company_name="ACME",
                   owner_id="98", sla_date=None, hde_link="https://hde/t/213746", link_staff="")


@pytest.fixture
def replayed(monkeypatch):
    refresh._STAFF_GROUP_CACHE.clear()
    calls = []

    async def fake_handle(bot, payload):
        calls.append(payload)

    monkeypatch.setattr(refresh, "handle_client_reply", fake_handle)
    return calls


def _with_topic(monkeypatch, **kwargs):
    topic = make_topic(ticket_id="213746", created_at=to_storage(utcnow() - timedelta(hours=2)), **kwargs)

    async def get_topic(ticket_id):
        return topic

    monkeypatch.setattr("bot.db.get_topic", get_topic)


@pytest.mark.asyncio
async def test_unanswered_client_tail_is_replayed_oldest_first(monkeypatch, replayed):
    _with_topic(monkeypatch, last_client_reply_at=to_storage(utcnow() - timedelta(minutes=60)))
    client = FakeClient(
        [_post(1, STAFF, 30), _post(2, CLIENT, 20, "Минуту"), _post(3, CLIENT, 10, "Получилось!)<br />\nСпасибо")],
        {str(STAFF): "staff", str(CLIENT): "user"},
    )

    assert await refresh._replay_missed_client_posts(None, client, TICKET, "Игорь") == 2
    assert [p["message"] for p in replayed] == ["Минуту", "Получилось!)\nСпасибо"]
    assert replayed[0]["ticket_id"] == "213746" and replayed[0]["owner_name"] == "Игорь"
    assert replayed[0]["last_post_date"] < replayed[1]["last_post_date"]


@pytest.mark.asyncio
async def test_posts_the_bot_already_saw_are_not_replayed(monkeypatch, replayed):
    _with_topic(monkeypatch, last_client_reply_at=to_storage(utcnow() - timedelta(minutes=5)))
    client = FakeClient([_post(1, STAFF, 30), _post(2, CLIENT, 10)], {str(STAFF): "staff", str(CLIENT): "user"})

    assert await refresh._replay_missed_client_posts(None, client, TICKET, "Игорь") == 0
    assert replayed == []


@pytest.mark.asyncio
async def test_failed_author_lookup_is_treated_as_staff(monkeypatch, replayed):
    _with_topic(monkeypatch)
    client = FakeClient([_post(1, CLIENT, 10)], {})  # HDE lookup returns None

    assert await refresh._replay_missed_client_posts(None, client, TICKET, "Игорь") == 0
    assert replayed == []


@pytest.mark.asyncio
async def test_fresh_post_is_left_to_the_webhook(monkeypatch, replayed):
    _with_topic(monkeypatch)
    client = FakeClient([_post(1, CLIENT, 0.2)], {str(CLIENT): "user"})

    assert await refresh._replay_missed_client_posts(None, client, TICKET, "Игорь") == 0
    assert replayed == []


def test_storage_date_is_shown_in_msk():
    assert _parse_hde_post_date("2026-10-12 06:44:27") == "12.10 09:44"
    assert _parse_hde_post_date("09:44:27 12.10.2026") == "12.10 09:44"
