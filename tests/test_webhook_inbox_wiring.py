"""Wiring: webhook сохраняет событие durable ДО ACK, воркер дренит inbox.

ADR I4/I5: 200 OK = событие надёжно сохранено (pending), обработано — только
после 'completed'. Падение процесса после ACK не теряет событие (его подхватит
следующий проход воркера)."""
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

import bot.db as db_module
import bot.hde_webhook as wh


@pytest.fixture(autouse=True)
async def _init_db():
    await db_module.init_db()


def _req(payload: dict):
    r = AsyncMock(spec=web.Request)
    r.app = {"bot": AsyncMock()}
    r.remote = "127.0.0.1"
    r.json = AsyncMock(return_value=payload)
    return r


_PAYLOAD = {
    "event_type": "client_reply", "ticket_id": "T1",
    "date_update": "2026-07-12 10:00:00", "secret": "test_secret",
}


async def test_webhook_enqueues_durably_before_ack(monkeypatch):
    async def no_drain(bot, **k):        # kick отключён → проверяем именно durable-сохранение
        return {}
    monkeypatch.setattr(wh, "drain_inbox", no_drain)

    resp = await wh.hde_webhook_handler(_req(dict(_PAYLOAD)))
    assert resp.status == 200
    # событие сохранено (pending), ещё НЕ обработано
    assert (await db_module.count_inbox_by_status()).get("pending") == 1


async def test_webhook_duplicate_delivery_acks_without_second_enqueue(monkeypatch):
    async def no_drain(bot, **k):
        return {}
    monkeypatch.setattr(wh, "drain_inbox", no_drain)

    r1 = await wh.hde_webhook_handler(_req(dict(_PAYLOAD)))
    r2 = await wh.hde_webhook_handler(_req(dict(_PAYLOAD)))
    assert r1.status == 200 and r2.status == 200
    assert r2.text == "Duplicate OK"
    assert (await db_module.count_inbox_by_status()).get("pending") == 1


async def test_webhook_rejects_bad_secret_without_enqueue(monkeypatch):
    async def no_drain(bot, **k):
        return {}
    monkeypatch.setattr(wh, "drain_inbox", no_drain)
    bad = dict(_PAYLOAD, secret="wrong")
    resp = await wh.hde_webhook_handler(_req(bad))
    assert resp.status == 403
    assert await db_module.count_inbox_by_status() == {}   # ничего не сохранили


async def test_drain_dispatches_and_completes():
    await db_module.enqueue_event(
        "E1", json.dumps({"event_type": "client_reply", "ticket_id": "T1"})
    )
    seen = {}

    async def fake_dispatch(bot, payload):
        seen["payload"] = payload

    stats = await wh.drain_inbox(AsyncMock(), _dispatch_fn=fake_dispatch)
    assert seen["payload"]["ticket_id"] == "T1"
    assert stats["processed"] == 1
    assert (await db_module.get_inbox_event("E1"))["status"] == "completed"


async def test_drain_marks_failed_and_retryable_on_exception():
    await db_module.enqueue_event(
        "E1", json.dumps({"event_type": "client_reply", "ticket_id": "T1"})
    )

    async def boom(bot, payload):
        raise RuntimeError("handler down")

    stats = await wh.drain_inbox(AsyncMock(), _dispatch_fn=boom)
    assert stats["failed"] == 1
    ev = await db_module.get_inbox_event("E1")
    assert ev["status"] == "pending"        # не completed → будет ретрай с backoff
    assert ev["attempts"] == 1
