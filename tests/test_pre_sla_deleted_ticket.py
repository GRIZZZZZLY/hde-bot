"""Тикет удалён в HDE: pre-SLA не шлём, топик удаляем.

HDE не шлёт вебхук на удаление и отдаёт удалённый тикет как открытый
с deleted=1 — бот узнаёт об удалении только спросив сам.
"""
from types import SimpleNamespace

import pytest

from bot import scheduler
from bot import work_schedule


def _record():
    return SimpleNamespace(
        ticket_id="209994", chat_id=-100, topic_id=7,
        last_client_reply_at=None, pre_sla_sent_at=None,
    )


@pytest.fixture
def due_one(monkeypatch):
    record = _record()
    calls = {"alert": 0, "deleted": []}

    async def due(_now):
        return [record]

    async def empty(*_a):
        return []

    async def alert(_bot, _rec):
        calls["alert"] += 1

    async def delete(_bot, rec):
        calls["deleted"].append(rec.ticket_id)
        return True

    async def not_replied(*_a):
        return False

    monkeypatch.setattr(scheduler.db, "list_due_pre_sla", due)
    monkeypatch.setattr(scheduler.db, "list_active_pre_sla", empty)
    monkeypatch.setattr(scheduler.db, "list_due_deletions", empty)
    monkeypatch.setattr(scheduler, "send_pre_sla_alert", alert)
    monkeypatch.setattr(scheduler, "delete_pending_topic", delete)
    monkeypatch.setattr(scheduler, "_hde_staff_replied_since", not_replied)
    monkeypatch.setattr(work_schedule, "is_work_time_for", lambda _op: True)
    return calls


def _hde_status(monkeypatch, result):
    async def status(self, _ticket_id):
        return result
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_ticket_open_status", status)


@pytest.mark.asyncio
async def test_deleted_ticket_drops_alert_and_topic(monkeypatch, due_one):
    _hde_status(monkeypatch, (True, ""))
    await scheduler._process_due_timers(object())
    assert due_one["alert"] == 0
    assert due_one["deleted"] == ["209994"]


@pytest.mark.asyncio
async def test_open_ticket_still_gets_alert(monkeypatch, due_one):
    _hde_status(monkeypatch, (False, ""))
    await scheduler._process_due_timers(object())
    assert due_one["alert"] == 1
    assert due_one["deleted"] == []


@pytest.mark.asyncio
async def test_hde_error_still_sends_alert(monkeypatch, due_one):
    _hde_status(monkeypatch, None)
    await scheduler._process_due_timers(object())
    assert due_one["alert"] == 1
    assert due_one["deleted"] == []
