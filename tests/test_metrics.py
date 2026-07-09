"""Tests for in-memory ops metrics exposed via /health."""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock, patch
from aiohttp import web

import bot.db as db_module
import bot.hde_webhook as hde_webhook_module
from bot import metrics
from bot.hde_webhook import hde_webhook_handler
from bot.main import _health_handler


@pytest.fixture(autouse=True)
def reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


def test_inc_and_snapshot():
    metrics.inc("webhook_received")
    metrics.inc("webhook_received")
    metrics.inc("webhook_duplicate")

    snap = metrics.snapshot()

    assert snap["webhook_received"] == 2
    assert snap["webhook_duplicate"] == 1
    assert snap["uptime_seconds"] >= 0


def test_llm_latency_average():
    metrics.observe_llm_latency(0.2)
    metrics.observe_llm_latency(0.4)

    snap = metrics.snapshot()

    assert snap["llm_latency_avg_ms"] == 300


@pytest.mark.asyncio
async def test_health_returns_json_with_metrics():
    metrics.inc("webhook_processed")

    response = await _health_handler(None)

    assert response.status == 200
    body = json.loads(response.text)
    assert body["status"] == "ok"
    assert body["webhook_processed"] == 1


def make_request(payload: dict) -> AsyncMock:
    request = AsyncMock(spec=web.Request)
    request.app = {"bot": AsyncMock()}
    request.remote = "127.0.0.1"
    request.json = AsyncMock(return_value=payload)
    return request


PAYLOAD = {
    "event_type": "client_reply",
    "ticket_id": "TKT-METRICS",
    "unique_id": "U-METRICS",
    "date_update": "2026-07-07 12:00:00",
    "secret": "test_secret",
}


async def drain() -> None:
    while hde_webhook_module._background_tasks:
        await asyncio.gather(*list(hde_webhook_module._background_tasks))


@pytest.mark.asyncio
async def test_webhook_counters_processed_and_duplicate():
    await db_module.init_db()

    with patch.dict("bot.hde_webhook.HANDLERS", {"client_reply": AsyncMock()}):
        await hde_webhook_handler(make_request(dict(PAYLOAD)))
        await drain()
        await hde_webhook_handler(make_request(dict(PAYLOAD)))
        await drain()

    snap = metrics.snapshot()
    assert snap["webhook_received"] == 2
    assert snap["webhook_processed"] == 1
    assert snap["webhook_duplicate"] == 1


@pytest.mark.asyncio
async def test_webhook_counter_failed():
    await db_module.init_db()

    failing = AsyncMock(side_effect=RuntimeError("boom"))
    payload = dict(PAYLOAD, ticket_id="TKT-METRICS-2", date_update="2026-07-07 13:00:00")
    with patch.dict("bot.hde_webhook.HANDLERS", {"client_reply": failing}):
        await hde_webhook_handler(make_request(payload))
        await drain()

    snap = metrics.snapshot()
    assert snap["webhook_failed"] == 1
    assert snap.get("webhook_processed", 0) == 0
