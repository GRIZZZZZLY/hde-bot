"""Tests for the shared call_groq_text helper and its use in _maybe_update_pattern."""
import pytest
from unittest.mock import AsyncMock

import bot.ai_summary as ai_summary_module
import bot.db as db_module
import bot.topic_manager as topic_manager_module
from bot import metrics
from bot.ai_summary import call_groq_text


@pytest.fixture(autouse=True)
def reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.last_json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.last_json = kwargs.get("json")
        return self._response


def _wire(monkeypatch, response):
    session = FakeSession(response)
    monkeypatch.setattr(ai_summary_module, "shared_session", lambda: session)
    monkeypatch.setattr(ai_summary_module.config, "groq_api_key", "test-key", raising=False)
    return session


@pytest.mark.asyncio
async def test_call_groq_text_returns_stripped_text(monkeypatch):
    session = _wire(
        monkeypatch,
        FakeResponse(200, {"choices": [{"message": {"content": "  ответ  "}}]}),
    )

    result = await call_groq_text("вопрос", max_tokens=123, temperature=0.5)

    assert result == "ответ"
    assert session.last_json["max_tokens"] == 123
    assert session.last_json["temperature"] == 0.5
    assert metrics.snapshot()["llm_calls"] == 1


@pytest.mark.asyncio
async def test_call_groq_text_none_on_http_error(monkeypatch):
    _wire(monkeypatch, FakeResponse(500, {}))

    result = await call_groq_text("вопрос")

    assert result is None
    assert metrics.snapshot()["llm_failures"] == 1


@pytest.mark.asyncio
async def test_call_groq_text_none_without_api_key(monkeypatch):
    monkeypatch.setattr(ai_summary_module.config, "groq_api_key", "", raising=False)

    assert await call_groq_text("вопрос") is None


@pytest.mark.asyncio
async def test_maybe_update_pattern_saves_new_pattern(monkeypatch):
    monkeypatch.setattr(db_module, "list_solution_patterns", AsyncMock(return_value=[]))
    monkeypatch.setattr(db_module, "pattern_exists_similar", AsyncMock(return_value=False))
    save = AsyncMock()
    monkeypatch.setattr(db_module, "save_solution_pattern", save)
    monkeypatch.setattr(
        ai_summary_module,
        "call_groq_text",
        AsyncMock(
            return_value='{"problem_type": "не печатает чек", "steps": "перезагрузка → драйвер"}'
        ),
    )

    await topic_manager_module._maybe_update_pattern(
        "Касса АТОЛ не печатает", "Перезагрузите кассу и проверьте драйвер", "T1"
    )

    assert save.await_count == 1
    assert save.await_args.kwargs["problem_type"] == "не печатает чек"
    assert save.await_args.kwargs["steps"] == "перезагрузка → драйвер"
