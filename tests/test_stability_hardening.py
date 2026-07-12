"""Tests for stability hardening: WAL mode, busy_timeout, durable-inbox webhook dedup, fail-closed secret."""
import asyncio
import datetime as _dt
import json

import aiosqlite
import pytest
from unittest.mock import AsyncMock, patch
from aiohttp import web

import bot.config as config_module
import bot.db as db_module
from bot.db import core as db_core
from bot.hde_webhook import hde_webhook_handler


def make_request(payload: dict) -> AsyncMock:
    request = AsyncMock(spec=web.Request)
    request.app = {"bot": AsyncMock()}
    request.remote = "127.0.0.1"
    request.json = AsyncMock(return_value=payload)
    return request


CLIENT_REPLY_PAYLOAD = {
    "event_type": "client_reply",
    "ticket_id": "TKT-100",
    "unique_id": "U-100",
    "date_update": "2026-07-06 10:00:00",
    "secret": "test_secret",
}


@pytest.mark.asyncio
async def test_init_db_enables_wal(set_test_db):
    await db_module.init_db()
    async with aiosqlite.connect(set_test_db) as db:
        async with db.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
    assert row[0].lower() == "wal"


@pytest.mark.asyncio
async def test_connect_sets_busy_timeout():
    await db_module.init_db()
    async with db_core.connect() as db:
        async with db.execute("PRAGMA busy_timeout") as cursor:
            row = await cursor.fetchone()
    assert row[0] == 5000


@pytest.mark.asyncio
async def test_duplicate_event_during_processing_is_skipped():
    await db_module.init_db()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_handler(bot, payload):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    import bot.hde_webhook as hde_webhook_module

    with patch.dict("bot.hde_webhook.HANDLERS", {"client_reply": slow_handler}):
        first = asyncio.create_task(
            hde_webhook_handler(make_request(dict(CLIENT_REPLY_PAYLOAD)))
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        duplicate = await asyncio.wait_for(
            hde_webhook_handler(make_request(dict(CLIENT_REPLY_PAYLOAD))), timeout=2
        )
        release.set()
        original = await first
        while hde_webhook_module._background_tasks:
            await asyncio.gather(*list(hde_webhook_module._background_tasks))

    assert original.status == 200
    assert duplicate.status == 200
    assert duplicate.text == "Duplicate OK"
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_handler_retried_by_worker_not_hde_redelivery():
    """Durable-inbox контракт: провал хендлера НЕ теряет событие — оно остаётся
    pending с backoff и повторяется воркером. Повторная доставка HDE того же
    события дедупится (без двойной обработки); ретрай приходит от воркера."""
    await db_module.init_db()
    calls = 0

    async def failing_then_ok(bot, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")

    import bot.hde_webhook as hde_webhook_module

    async def drain():
        while hde_webhook_module._background_tasks:
            await asyncio.gather(*list(hde_webhook_module._background_tasks))

    with patch.dict("bot.hde_webhook.HANDLERS", {"client_reply": failing_then_ok}):
        first = await hde_webhook_handler(make_request(dict(CLIENT_REPLY_PAYLOAD)))
        await drain()
        assert calls == 1                                      # первая попытка упала
        # событие не потеряно — pending, готово к ретраю
        assert (await db_module.count_inbox_by_status()).get("pending") == 1

        # повторная доставка HDE того же события → дедуп, без второй обработки
        retry = await hde_webhook_handler(make_request(dict(CLIENT_REPLY_PAYLOAD)))
        await drain()
        assert retry.text == "Duplicate OK"
        assert calls == 1

        # воркер ретраит после backoff (эмулируем истёкший backoff через now)
        future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=120)
        row = await db_module.claim_next_event(now=future)
        assert row is not None
        payload = json.loads(row["payload"])
        await hde_webhook_module.dispatch_event(AsyncMock(), payload)
        await db_module.mark_completed(row["event_id"])

    assert first.status == 200
    assert retry.status == 200
    assert calls == 2                                          # ретрай воркера прошёл
    assert (await db_module.get_inbox_event(row["event_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_webhook_rejects_when_secret_not_configured(monkeypatch):
    await db_module.init_db()
    monkeypatch.setattr(config_module.config, "hde_webhook_secret", "")

    handler_mock = AsyncMock()
    with patch.dict("bot.hde_webhook.HANDLERS", {"client_reply": handler_mock}):
        response = await hde_webhook_handler(make_request(dict(CLIENT_REPLY_PAYLOAD)))

    assert response.status == 403
    handler_mock.assert_not_called()


def test_startup_requires_webhook_secret(monkeypatch):
    from bot.main import _ensure_webhook_secret

    monkeypatch.setattr(config_module.config, "hde_webhook_secret", "")
    with pytest.raises(SystemExit):
        _ensure_webhook_secret()


def test_startup_passes_with_webhook_secret():
    from bot.main import _ensure_webhook_secret

    _ensure_webhook_secret()
