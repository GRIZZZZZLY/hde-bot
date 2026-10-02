"""Tests for the daily AI-value report (roadmap rev.3)."""
from unittest.mock import AsyncMock

import bot.db as db_module
from bot.agent.value_report import format_value_report, send_daily_value_report
from bot.db.suggestion_store import (
    collect_suggestion_daily_stats,
    record_suggestion,
    record_suggestion_event,
)
from bot.config import config


async def _seed(topic_id: int, ctx: str) -> int:
    return await record_suggestion(
        ticket_id=f"T{topic_id}", topic_id=topic_id, chat_id=config.group_chat_id, trigger_source="first",
        context_until_post_id=ctx, pipeline_version="v0", prompt_version="legacy",
    )


async def test_collect_stats_counts_actions():
    await db_module.init_db()
    s1 = await _seed(1, "1")                      # 📤 без правки
    await record_suggestion_event(s1, "send_requested")
    await record_suggestion_event(s1, "sent", payload="текст")
    s2 = await _seed(2, "2")                      # ✏️ исправлено, потом 📤
    await record_suggestion_event(s2, "edited", payload="правка")
    await record_suggestion_event(s2, "send_requested")
    await record_suggestion_event(s2, "sent", payload="правка")
    s3 = await _seed(3, "3")                      # 👎
    await record_suggestion_event(s3, "rejected")
    await _seed(4, "4")                           # без реакции

    stats = await collect_suggestion_daily_stats(24)
    assert stats["total"] == 4
    assert stats["sent_no_edit"] == 1
    assert stats["sent_edited"] == 1
    assert stats["edited"] == 1
    assert stats["rejected"] == 1
    assert stats["unused"] == 1
    assert stats["avg_minutes_to_send"] is not None


def test_format_value_report_contains_counts_and_share():
    text = format_value_report({
        "total": 4, "sent_no_edit": 1, "sent_edited": 1, "edited": 1,
        "rejected": 1, "approved": 0, "unused": 1, "avg_minutes_to_send": 12.4,
    })
    assert "Всего подсказок: 4" in text
    assert "без правки: 1" in text
    assert "Доля 📤 без правки: 50%" in text
    assert "12 мин" in text


async def test_send_skips_empty_day():
    bot = AsyncMock()

    async def empty_stats(hours):
        return {"total": 0}

    sent = await send_daily_value_report(bot, _stats_fn=empty_stats)
    assert sent is False
    bot.send_message.assert_not_awaited()


async def test_send_posts_report_when_data(monkeypatch):
    bot = AsyncMock()

    async def stats(hours):
        return {"total": 2, "sent_no_edit": 1, "sent_edited": 0, "edited": 0,
                "rejected": 1, "approved": 0, "unused": 0,
                "avg_minutes_to_send": None}

    sent = await send_daily_value_report(bot, _stats_fn=stats)
    assert sent is True
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.await_args
    assert "AI-подсказки" in args[1]
