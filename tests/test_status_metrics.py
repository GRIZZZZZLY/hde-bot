"""cmd_status includes ops metrics (webhook/LLM counters) alongside topic counts."""
import pytest
from unittest.mock import AsyncMock, MagicMock

import bot.handlers.commands as cmds
from bot import metrics


@pytest.fixture(autouse=True)
def reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.mark.asyncio
async def test_status_shows_webhook_and_llm_metrics(monkeypatch):
    monkeypatch.setattr(cmds, "count_active_topics", AsyncMock(return_value=3))
    monkeypatch.setattr(cmds, "count_pending_delete_topics", AsyncMock(return_value=1))
    monkeypatch.setattr(cmds, "count_pending_pre_sla_topics", AsyncMock(return_value=2))
    monkeypatch.setattr(cmds, "count_total_topics", AsyncMock(return_value=9))

    metrics.inc("webhook_received", 5)
    metrics.inc("webhook_failed", 2)
    metrics.inc("llm_calls", 4)
    metrics.observe_llm_latency(0.5)

    message = MagicMock()
    message.answer = AsyncMock()

    await cmds.cmd_status(message)

    sent = str(message.answer.await_args.args[0])
    # topic counts still present
    assert "Активных topics" in sent and "3" in sent
    # webhook + llm metrics present
    assert "5" in sent          # webhook_received
    assert "2" in sent          # webhook_failed
    assert "500" in sent        # llm latency avg ms
