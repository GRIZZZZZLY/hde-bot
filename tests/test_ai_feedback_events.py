from unittest.mock import AsyncMock, MagicMock

import bot.db as db_module
from bot.config import config
from bot.db.suggestion_store import get_open_suggestion_by_topic, get_suggestion, record_suggestion
from bot.handlers.ai_feedback import _record_event, cb_send_to_hde, register_feedback_pending


async def test_record_event_maps_topic_to_latest_suggestion():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="T", topic_id=42, chat_id=config.group_chat_id, trigger_source="first",
        context_until_post_id="5", pipeline_version="v0", prompt_version="legacy",
    )
    await _record_event(config.group_chat_id, 42, "approved")
    row = await get_suggestion(sid)
    assert row["review_status"] == "approved"
    assert row["human_label"] == "accepted"


async def test_record_event_noop_without_suggestion():
    await db_module.init_db()
    # no suggestion for this topic — must not raise
    await _record_event(config.group_chat_id, 999, "approved")


def _fake_callback(topic_id: int, data: str = "ai:send_post") -> MagicMock:
    message = MagicMock()
    message.message_thread_id = topic_id
    message.chat.id = config.group_chat_id
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
        ticket_id="T3", topic_id=77, chat_id=config.group_chat_id, trigger_source="first",
        context_until_post_id="5", pipeline_version="v0", prompt_version="legacy",
    )
    # No ai_feedback_pending saved for topic 77 → "pending" guard triggers.
    callback = _fake_callback(77)
    await cb_send_to_hde(callback)

    row = await get_suggestion(sid)
    # Default delivery_status ("not_sent") must be untouched — no "requested"
    # event was recorded, since the pending-lookup guard short-circuited first.
    assert row["delivery_status"] == "not_sent"


def _send_callback(shown_text: str) -> MagicMock:
    callback = _fake_callback(78)
    callback.message.text = shown_text
    callback.message.caption = None
    callback.message.html_text = shown_text
    callback.message.edit_text = AsyncMock()
    return callback


async def _press_send(shown_text: str, answer_text: str):
    from unittest.mock import patch
    hde = MagicMock()
    hde.add_post = AsyncMock()
    pending = {"ticket_id": "T78", "answer_text": answer_text, "title": "t", "history": "h"}
    callback = _send_callback(shown_text)
    with (
        patch("bot.handlers.ai_feedback.get_ai_feedback_pending", new=AsyncMock(return_value=pending)),
        patch("bot.handlers.ai_feedback._record_event", new=AsyncMock()) as events,
        patch("bot.hde_api.HDEApiClient", return_value=hde),
        patch.object(db_module, "save_optimization_sample", new=AsyncMock(), create=True),
    ):
        await cb_send_to_hde(callback)
    return hde, callback, events


async def test_send_posts_the_draft_the_operator_sees():
    """Final review F4: переносы строк в Telegram не мешают сверке."""
    hde, _cb, _ev = await _press_send(
        "👤 Клиент\nкасса не печатает\n────────\n💡 Перезагрузите   кассу.\nПолучилось?",
        "Перезагрузите кассу. Получилось?",
    )
    hde.add_post.assert_awaited_once_with("T78", "Перезагрузите кассу. Получилось?")


async def test_send_refuses_a_draft_the_operator_did_not_see():
    """Final review F4: pending уже от другого черновика — клиенту ничего не уходит."""
    hde, callback, events = await _press_send(
        "💡 Перезагрузите кассу.", "Смените порт в настройках.",
    )
    hde.add_post.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "⚠️ Черновик устарел — нажми 🔄 для нового варианта", show_alert=True)
    assert [c.args[2] for c in events.await_args_list] == ["send_refused_stale"]


async def test_v2_correction_is_captured_only_after_edit_button(monkeypatch):
    """Final review F8: на v2 pending есть у каждого черновика — без ✏️ любое
    сообщение оператора в топике ушло бы в базу как «исправленный пример»."""
    from types import SimpleNamespace
    from unittest.mock import patch

    import bot.handlers.ai_feedback as fb

    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    monkeypatch.setattr(fb, "_awaiting_correction", set())
    message = SimpleNamespace(message_thread_id=79, chat=SimpleNamespace(id=config.group_chat_id),
                              from_user=SimpleNamespace(is_bot=False))
    pending = {"ticket_id": "T79", "answer_text": "a", "title": "t", "history": "h"}
    with (
        patch("bot.handlers.ai_feedback.get_ai_feedback_pending", new=AsyncMock(return_value=pending)),
        patch("bot.handlers.ai_feedback._record_event", new=AsyncMock()),
    ):
        assert await fb._HasPendingCorrection()(message) is False
        callback = _fake_callback(79, "ai:edit")
        callback.message.answer = AsyncMock()
        await fb.cb_ai_edit(callback)
        assert await fb._HasPendingCorrection()(message) is True
        monkeypatch.setattr(config, "agent_voice_v2_enabled", False)
        fb._awaiting_correction.clear()
        assert await fb._HasPendingCorrection()(message) is True   # флаг выключен — как раньше


async def test_new_pending_draft_cancels_an_unanswered_edit_request(monkeypatch):
    """Residual R26: ✏️ нажали и ничего не написали, пришёл новый черновик —
    следующее сообщение оператора не должно уйти в базу как исправление."""
    from unittest.mock import patch

    import bot.handlers.ai_feedback as fb

    monkeypatch.setattr(fb, "_awaiting_correction", {(config.group_chat_id, 79)})
    with (
        patch("bot.handlers.ai_feedback.save_ai_feedback_pending", new=AsyncMock()),
        patch("bot.handlers.ai_feedback.record_suggestion", new=AsyncMock(return_value=1)),
    ):
        await fb.register_feedback_pending(config.group_chat_id, 79, "T79", "h", "t", answer_text="новый черновик")
    assert (config.group_chat_id, 79) not in fb._awaiting_correction


async def test_register_feedback_pending_records_suggestion():
    await db_module.init_db()
    await register_feedback_pending(
        chat_id=config.group_chat_id, topic_id=77, ticket_id="T77", history="диалог клиента",
        title="Не печатает чек", answer_text="Клиенту: проверьте бумагу",
        ai_full_text="Суть: ...\nКлиенту: проверьте бумагу",
        context_until_post_id="123",
    )
    row = await get_open_suggestion_by_topic(config.group_chat_id, 77)
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
        ticket_id="TS", topic_id=55, chat_id=config.group_chat_id, trigger_source="first",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
    )
    await cb_suit_good(_fake_callback(55, "suit:good"))
    await cb_memo_bad(_fake_callback(55, "memo:bad"))

    events = await _event_types_for(sid)
    assert "suit_good" in events and "memo_bad" in events   # фидбэк теперь пишется
    row = await get_suggestion(sid)
    assert row["review_status"] == "pending"   # Суть/Памятка не трогают статус ответа
    assert row["human_label"] is None
