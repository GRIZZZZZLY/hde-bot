# tests/test_suggest_button.py — Phase 3: multi-turn подсказка по кнопке «💡 Предложить ответ»
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot.config as config_module
import bot.db as db_module
from bot.handlers.ai_feedback import _suggest_in_flight, cb_ai_suggest


def _fake_callback(topic_id: int) -> MagicMock:
    message = MagicMock()
    message.message_thread_id = topic_id
    message.chat.id = config_module.config.group_chat_id
    callback = MagicMock()
    callback.message = message
    callback.data = "ai:suggest"
    callback.answer = AsyncMock()
    callback.bot = AsyncMock()
    return callback


async def _upsert_topic(ticket_id: str, topic_id: int) -> None:
    await db_module.upsert_topic(
        ticket_id, topic_id,
        unique_id="U-1", company_name="ACME", ticket_name="Не печатает чек",
        priority="high", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        chat_id=config_module.config.group_chat_id,
    )


def _fake_hde_client(posts):
    client = MagicMock()
    client.get_ticket_info = AsyncMock(return_value=SimpleNamespace(client_id=1))
    client.get_ticket_posts = AsyncMock(return_value=posts)
    client.get_ticket_comments = AsyncMock(return_value=[])
    return client


_POSTS = [
    SimpleNamespace(post_id=5, date_created="2026-07-01 10:00:00", user_id=1, text="вопрос"),
    SimpleNamespace(post_id=9, date_created="2026-07-02 10:00:00", user_id=1, text="ещё вопрос"),
]


async def test_suggest_click_generates_with_fresh_history_and_button_source():
    await db_module.init_db()
    await _upsert_topic("TKT-9", 555)
    callback = _fake_callback(555)

    with patch("bot.hde_api.HDEApiClient", return_value=_fake_hde_client(_POSTS)), patch(
        "bot.topic_manager._generate_summary_with_retry",
        new=AsyncMock(return_value=("суть", "клиенту", "памятка", 80)),
    ) as gen, patch(
        "bot.topic_history.post_suggestion_messages", new=AsyncMock(return_value=True)
    ) as post:
        await cb_ai_suggest(callback)

    gen.assert_awaited_once()
    assert gen.await_args.kwargs["trigger_source"] == "button"
    assert gen.await_args.kwargs["ticket_id"] == "TKT-9"
    assert gen.await_args.kwargs["topic_id"] == 555
    # свежая история из HDE, не кэш
    assert [p.post_id for p in gen.await_args.args[0]] == [5, 9]

    post.assert_awaited_once()
    assert post.await_args.kwargs["trigger_source"] == "button"
    assert post.await_args.kwargs["anchor"] == "9"           # якорь = последний пост
    assert post.await_args.kwargs["ticket_id"] == "TKT-9"


async def test_second_click_while_generating_is_rejected():
    await db_module.init_db()
    await _upsert_topic("TKT-10", 556)
    callback = _fake_callback(556)

    _suggest_in_flight.add("TKT-10")
    try:
        with patch(
            "bot.topic_manager._generate_summary_with_retry", new=AsyncMock()
        ) as gen:
            await cb_ai_suggest(callback)
        gen.assert_not_awaited()
        assert "⏳" in callback.answer.call_args.args[0]
    finally:
        _suggest_in_flight.discard("TKT-10")


async def test_in_flight_guard_released_after_generation():
    await db_module.init_db()
    await _upsert_topic("TKT-11", 557)
    callback = _fake_callback(557)

    with patch("bot.hde_api.HDEApiClient", return_value=_fake_hde_client(_POSTS)), patch(
        "bot.topic_manager._generate_summary_with_retry",
        new=AsyncMock(return_value=("с", "к", "п", 50)),
    ), patch("bot.topic_history.post_suggestion_messages", new=AsyncMock(return_value=True)):
        await cb_ai_suggest(callback)

    assert "TKT-11" not in _suggest_in_flight


async def test_unknown_topic_alerts_and_skips_generation():
    await db_module.init_db()
    callback = _fake_callback(99999)

    with patch(
        "bot.topic_manager._generate_summary_with_retry", new=AsyncMock()
    ) as gen:
        await cb_ai_suggest(callback)
    gen.assert_not_awaited()
    assert callback.answer.call_args.kwargs.get("show_alert") is True


async def test_generation_failure_posts_error_notice():
    await db_module.init_db()
    await _upsert_topic("TKT-12", 558)
    callback = _fake_callback(558)

    with patch("bot.hde_api.HDEApiClient", return_value=_fake_hde_client(_POSTS)), patch(
        "bot.topic_manager._generate_summary_with_retry",
        new=AsyncMock(return_value=None),
    ), patch("bot.topic_history.post_suggestion_messages", new=AsyncMock()) as post:
        await cb_ai_suggest(callback)

    post.assert_not_awaited()
    # оператору сообщили о неудаче в топик
    sent_text = callback.bot.send_message.call_args.kwargs.get("text", "")
    assert "Не удалось" in sent_text
    assert "TKT-12" not in _suggest_in_flight


async def test_button_stays_only_under_latest_client_reply(monkeypatch):
    """Второй ответ клиента снимает 💡 с предыдущего сообщения и вешает на новое."""
    import bot.topic_manager as tm

    await db_module.init_db()
    await db_module.upsert_topic("TKT-B", 777, ticket_name="Касса", company_name="ACME", chat_id=config_module.config.group_chat_id)
    monkeypatch.setattr(tm, "_is_work_time", lambda *a, **k: True)

    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=[
        SimpleNamespace(message_id=101),
        SimpleNamespace(message_id=202),
    ])

    payload = {
        "ticket_id": "TKT-B", "unique_id": "B-1", "ticket_name": "Касса",
        "company_name": "ACME", "priority": "high", "status": "open",
        "owner_id": "me", "owner_name": "Me", "user_name": "Alice",
        "message": "раз", "last_post_date": "2026-07-01 10:00:00",
        "link": "https://hde.example.com/tickets/1",
    }

    await tm._handle_client_reply_locked(bot, payload, "TKT-B")
    rec = await db_module.get_topic("TKT-B")
    assert rec.suggest_button_msg_id == 101
    bot.edit_message_reply_markup.assert_not_awaited()  # первого сообщения ещё не было

    payload = {**payload, "message": "два", "last_post_date": "2026-07-01 11:00:00"}
    await tm._handle_client_reply_locked(bot, payload, "TKT-B")
    # кнопка снята со 101, новый msg_id = 202
    bot.edit_message_reply_markup.assert_awaited_once()
    assert bot.edit_message_reply_markup.await_args.kwargs["message_id"] == 101
    assert bot.edit_message_reply_markup.await_args.kwargs["reply_markup"] is None
    rec = await db_module.get_topic("TKT-B")
    assert rec.suggest_button_msg_id == 202


async def test_generate_with_retry_threads_trigger_source(monkeypatch):
    import bot.topic_manager as tm
    monkeypatch.setattr(config_module.config, "agent_enabled", True)
    monkeypatch.setattr(config_module.config, "agent_auto_first_suggestion_enabled", True)

    with patch("bot.topic_manager.run_agent", new=AsyncMock(
        return_value=("с", "к", "п", 70)
    )) as agent:
        await tm._generate_summary_with_retry(
            [SimpleNamespace(user_id=1, text="q", post_id=1)],
            SimpleNamespace(client_id=1),
            ticket_title="t", ticket_id="T-B", topic_id=3, chat_id=config_module.config.group_chat_id,
            trigger_source="button",
        )
    assert agent.await_args.kwargs["trigger_source"] == "button"
