"""Tests for resilience hardening: HDE GET backoff, systemd watchdog, ticket-lock eviction."""
import asyncio
import gc

import pytest
from unittest.mock import AsyncMock

import bot.hde_api as hde_api_module
import bot.main as main_module
import bot.topic_manager as topic_manager_module
from bot.hde_api import HDEApiClient


class FakeResponse:
    def __init__(self, status, body=None):
        self.status = status
        self.body = body if body is not None else {"data": []}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """Yields queued items per get(): FakeResponse is returned, Exception is raised."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, params=None):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(monkeypatch, items):
    # conftest zeroes the backoff base; restore it here — these tests assert
    # the sleep delays themselves (sleep is faked, so nothing actually waits).
    monkeypatch.setattr(HDEApiClient, "_GET_BACKOFF_BASE", 0.5)
    client = HDEApiClient()
    session = FakeSession(items)
    monkeypatch.setattr(client, "_make_session", lambda: session)

    async def fake_read(response):
        return response.body

    monkeypatch.setattr(client, "_read_response", fake_read)
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(hde_api_module.asyncio, "sleep", fake_sleep)
    return client, session, sleeps


@pytest.mark.asyncio
async def test_get_retries_on_5xx_and_succeeds(monkeypatch):
    client, session, sleeps = make_client(
        monkeypatch,
        [FakeResponse(500), FakeResponse(502), FakeResponse(200, {"ok": True})],
    )

    status, body = await client._get("/tickets/1/")

    assert status == 200
    assert body == {"ok": True}
    assert session.calls == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]


@pytest.mark.asyncio
async def test_get_returns_last_5xx_after_max_attempts(monkeypatch):
    client, session, _ = make_client(
        monkeypatch,
        [FakeResponse(503), FakeResponse(503), FakeResponse(503)],
    )

    status, _body = await client._get("/tickets/1/")

    assert status == 503
    assert session.calls == 3


@pytest.mark.asyncio
async def test_get_retries_on_timeout(monkeypatch):
    client, session, _ = make_client(
        monkeypatch,
        [asyncio.TimeoutError(), FakeResponse(200, {"ok": True})],
    )

    status, body = await client._get("/tickets/1/")

    assert status == 200
    assert session.calls == 2


@pytest.mark.asyncio
async def test_get_raises_after_max_timeout_attempts(monkeypatch):
    client, session, _ = make_client(
        monkeypatch,
        [asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()],
    )

    with pytest.raises(asyncio.TimeoutError):
        await client._get("/tickets/1/")
    assert session.calls == 3


@pytest.mark.asyncio
async def test_get_retries_on_429(monkeypatch):
    client, session, _ = make_client(
        monkeypatch,
        [FakeResponse(429), FakeResponse(200, {"ok": True})],
    )

    status, _body = await client._get("/tickets/1/")

    assert status == 200
    assert session.calls == 2


@pytest.mark.asyncio
async def test_watchdog_loop_pings_and_stops(monkeypatch):
    pings: list[str] = []
    monkeypatch.setattr(main_module, "_sd_notify", pings.append)
    stop_event = asyncio.Event()

    task = asyncio.create_task(main_module._watchdog_loop(stop_event, interval=0.01))
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert pings.count("WATCHDOG=1") >= 2


def test_sd_notify_is_noop_without_notify_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    main_module._sd_notify("WATCHDOG=1")  # must not raise


@pytest.mark.asyncio
async def test_ticket_lock_same_object_while_referenced():
    lock1 = topic_manager_module._ticket_lock("TKT-LOCK-1")
    lock2 = topic_manager_module._ticket_lock("TKT-LOCK-1")
    assert lock1 is lock2


@pytest.mark.asyncio
async def test_ticket_lock_evicted_when_unreferenced():
    lock = topic_manager_module._ticket_lock("TKT-LOCK-2")
    assert "TKT-LOCK-2" in topic_manager_module._ticket_locks
    del lock
    gc.collect()
    assert "TKT-LOCK-2" not in topic_manager_module._ticket_locks
