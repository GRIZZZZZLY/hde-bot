"""Tests for speed hardening: instant webhook ACK, shared HTTP connector, off-loop file logging."""
import asyncio
import builtins
import json
import threading

import pytest
from unittest.mock import AsyncMock, patch
from aiohttp import web

import bot.db as db_module
import bot.hde_webhook as hde_webhook_module
from bot import ai_summary as ai_summary_module
from bot import ticket_fields as ticket_fields_module
from bot.hde_api import _get_connector, shared_session
from bot.hde_webhook import hde_webhook_handler


def make_request(payload: dict) -> AsyncMock:
    request = AsyncMock(spec=web.Request)
    request.app = {"bot": AsyncMock()}
    request.remote = "127.0.0.1"
    request.json = AsyncMock(return_value=payload)
    return request


CLIENT_REPLY_PAYLOAD = {
    "event_type": "client_reply",
    "ticket_id": "TKT-200",
    "unique_id": "U-200",
    "date_update": "2026-07-06 12:00:00",
    "secret": "test_secret",
}


async def drain_background_tasks() -> None:
    while hde_webhook_module._background_tasks:
        await asyncio.gather(*list(hde_webhook_module._background_tasks))


@pytest.mark.asyncio
async def test_webhook_acks_before_handler_completes():
    await db_module.init_db()
    release = asyncio.Event()
    calls = 0

    async def slow_handler(bot, payload):
        nonlocal calls
        calls += 1
        await release.wait()

    with patch.dict("bot.hde_webhook.HANDLERS", {"client_reply": slow_handler}):
        response = await asyncio.wait_for(
            hde_webhook_handler(make_request(dict(CLIENT_REPLY_PAYLOAD))), timeout=2
        )
        assert response.status == 200
        release.set()
        await drain_background_tasks()

    assert calls == 1


@pytest.mark.asyncio
async def test_shared_session_reuses_connector():
    async with shared_session() as s1:
        async with shared_session() as s2:
            assert s1.connector is s2.connector
            assert s1.connector is _get_connector()
    assert not _get_connector().closed


def _spy_open(monkeypatch, path_suffix: str, threads: list):
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        if str(file).endswith(path_suffix):
            threads.append(threading.current_thread())
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)


@pytest.mark.asyncio
async def test_log_generation_writes_off_event_loop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    threads: list = []
    _spy_open(monkeypatch, "ai_log.jsonl", threads)

    await ai_summary_module._log_generation("TKT-1", "title", "history", "generated")

    assert threads, "log file was not written"
    assert all(t is not threading.main_thread() for t in threads)
    lines = (tmp_path / "data" / "ai_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["ticket_id"] == "TKT-1"


@pytest.mark.asyncio
async def test_log_env_outcome_writes_off_event_loop(tmp_path, monkeypatch):
    log_path = tmp_path / "env_corrections.jsonl"
    monkeypatch.setattr(ticket_fields_module, "ENV_CORRECTIONS_PATH", str(log_path))

    class FakeClient:
        async def get_ticket_field_value(self, ticket_id, field_id):
            return "Облако"

    monkeypatch.setattr(ticket_fields_module, "HDEApiClient", FakeClient)
    threads: list = []
    _spy_open(monkeypatch, "env_corrections.jsonl", threads)

    await ticket_fields_module.log_env_outcome("TKT-1", "Облако")

    assert threads, "log file was not written"
    assert all(t is not threading.main_thread() for t in threads)
    assert json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])["match"] is True


@pytest.mark.asyncio
async def test_log_pt_outcome_writes_off_event_loop(tmp_path, monkeypatch):
    log_path = tmp_path / "pt_corrections.jsonl"
    monkeypatch.setattr(ticket_fields_module, "PT_CORRECTIONS_PATH", str(log_path))

    class FakeClient:
        async def get_ticket_priority_type(self, ticket_id):
            return ("1", "0")

    monkeypatch.setattr(ticket_fields_module, "HDEApiClient", FakeClient)
    threads: list = []
    _spy_open(monkeypatch, "pt_corrections.jsonl", threads)

    await ticket_fields_module.log_pt_outcome("TKT-1", "1", "0")

    assert threads, "log file was not written"
    assert all(t is not threading.main_thread() for t in threads)
    assert json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])["match"] is True
