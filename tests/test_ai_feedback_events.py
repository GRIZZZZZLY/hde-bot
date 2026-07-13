from unittest.mock import AsyncMock, MagicMock

import bot.db as db_module
from bot.db.suggestion_store import get_open_suggestion_by_topic, get_suggestion, record_suggestion
from bot.handlers.ai_feedback import _record_event, cb_send_to_hde, register_feedback_pending


async def test_record_event_maps_topic_to_latest_suggestion():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="T", topic_id=42, trigger_source="first",
        context_until_post_id="5", pipeline_version="v0", prompt_version="legacy",
    )
    await _record_event(42, "approved")
    row = await get_suggestion(sid)
    assert row["review_status"] == "approved"
    assert row["human_label"] == "accepted"


async def test_record_event_noop_without_suggestion():
    await db_module.init_db()
    # no suggestion for this topic — must not raise
    await _record_event(999, "approved")


def _fake_callback(topic_id: int, data: str = "ai:send_post") -> MagicMock:
    message = MagicMock()
    message.message_thread_id = topic_id
    message.edit_reply_markup = AsyncMock()
    callback = MagicMock()
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def test_send_to_hde_stale_pending_records_no_send_requested():
    """Regression: early-return guards (stale TTL / empty answer_text) must NOT
    leave a dangling 'send_requested' event with no matching 'sent'/'send_failed'."""
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="T3", topic_id=77, trigger_source="first",
        context_until_post_id="5", pipeline_version="v0", prompt_version="legacy",
    )
    # No ai_feedback_pending saved for topic 77 → "pending" guard triggers.
    callback = _fake_callback(77)
    await cb_send_to_hde(callback)

    row = await get_suggestion(sid)
    # Default delivery_status ("not_sent") must be untouched — no "requested"
    # event was recorded, since the pending-lookup guard short-circuited first.
    assert row["delivery_status"] == "not_sent"


async def test_register_feedback_pending_records_suggestion():
    await db_module.init_db()
    await register_feedback_pending(
        topic_id=77, ticket_id="T77", history="диалог клиента",
        title="Не печатает чек", answer_text="Клиенту: проверьте бумагу",
        ai_full_text="Суть: ...\nКлиенту: проверьте бумагу",
        context_until_post_id="123",
    )
    row = await get_open_suggestion_by_topic(77)
    assert row is not None
    assert row["ticket_id"] == "T77"
    assert row["trigger_source"] == "first"
    assert row["context_until_post_id"] == "123"
    assert row["ai_full_text"].endswith("проверьте бумагу")


# --- Суть/Памятка фидбэк пишет события (Phase C fix 2026-07-13) ---
from bot.handlers.ai_feedback import cb_suit_good, cb_memo_bad
from bot.db.core import connect as _connect


async def _event_types_for(suggestion_id: int) -> list[str]:
    async with _connect() as db:
        cur = await db.execute(
            "SELECT event_type FROM ai_suggestion_events WHERE suggestion_id=? ORDER BY id",
            (suggestion_id,),
        )
        return [r[0] for r in await cur.fetchall()]


async def test_suit_and_memo_feedback_record_events_without_corrupting_answer_status():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="TS", topic_id=55, trigger_source="first",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
    )
    await cb_suit_good(_fake_callback(55, "suit:good"))
    await cb_memo_bad(_fake_callback(55, "memo:bad"))

    events = await _event_types_for(sid)
    assert "suit_good" in events and "memo_bad" in events   # фидбэк теперь пишется
    row = await get_suggestion(sid)
    assert row["review_status"] == "pending"   # Суть/Памятка не трогают статус ответа
    assert row["human_label"] is None
