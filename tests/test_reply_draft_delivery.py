from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot import topic_history
from bot.config import config
from bot.formatter import format_draft_block
from bot.hde_api import HDEPost, HDETicketInfo


def test_draft_block_escapes_html_and_hides_empty_memo():
    block = format_draft_block("Нажмите <OK> & ждите", "—")
    assert "&lt;OK&gt; &amp;" in block
    assert "<code>" in block and "📝" not in block


def test_draft_block_standalone_has_no_separator_and_suit_first():
    block = format_draft_block("c", "m", suit="Атол не печатает", separator=False)
    assert not block.startswith("\n")
    assert block.index("Атол не печатает") < block.index("<code>c</code>")


def _hde_client():
    client = MagicMock()
    client.get_ticket_info = AsyncMock(return_value=HDETicketInfo(1, "A", 2, "B"))
    client.get_ticket_posts = AsyncMock(return_value=[
        HDEPost(post_id=5, user_id=1, text="касса не печатает", date_created="10:00:00 01.01.2026")])
    client.get_ticket_comments = AsyncMock(return_value=[])
    return client


async def _run(monkeypatch, *, latest_msg_id, reply_html="👤 <b>Клиент</b>\n<blockquote>x</blockquote>",
               result=("s", "Перезагрузите кассу. Получилось?", "Атол • порт", 0),
               get_topic=None):
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=4242))
    with (
        patch("bot.hde_api.HDEApiClient", return_value=_hde_client()),
        patch("bot.topic_manager._generate_summary_with_retry", new=AsyncMock(return_value=result)),
        patch("bot.topic_manager.db") as db,
        patch("bot.handlers.ai_feedback.register_feedback_pending", new=AsyncMock()) as reg,
    ):
        db.get_topic = get_topic or AsyncMock(
            return_value=SimpleNamespace(suggest_button_msg_id=latest_msg_id))
        db.update_topic = AsyncMock()
        ok = await topic_history.append_draft_to_reply(
            bot, ticket_id="T", topic_id=10, message_id=77, reply_html=reply_html, ticket_title="t")
    return ok, bot, reg, db


async def test_draft_is_appended_to_latest_reply_with_keyboard(monkeypatch):
    ok, bot, reg, _db = await _run(monkeypatch, latest_msg_id=77)
    assert ok
    kwargs = bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 77 and kwargs["reply_markup"] is not None
    assert "<code>Перезагрузите кассу. Получилось?</code>" in kwargs["text"]
    reg.assert_awaited_once()


async def test_stale_draft_has_no_keyboard_and_no_pending(monkeypatch):
    """Review Focus 1: черновик на старый ответ пришёл после нового."""
    ok, bot, reg, _db = await _run(monkeypatch, latest_msg_id=99)
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None
    reg.assert_not_awaited()


async def test_overflow_goes_to_separate_silent_message(monkeypatch):
    """Review Focus 2: не влезает в 4096 — отдельное тихое сообщение.

    Fix round 1, finding 1: the 📤 button moves to this new message, so the
    old one (on the client-reply message, still the latest here) is stripped
    and the topic's suggest_button_msg_id now points at the new message.
    """
    ok, bot, reg, db = await _run(monkeypatch, latest_msg_id=77, reply_html="x" * 4090)
    bot.edit_message_text.assert_not_awaited()
    assert bot.send_message.await_args.kwargs["disable_notification"] is True
    assert bot.edit_message_reply_markup.await_args.kwargs["message_id"] == 77
    assert any(
        c.kwargs.get("suggest_button_msg_id") == 4242
        for c in db.update_topic.await_args_list
    )


async def test_no_action_appends_nothing(monkeypatch):
    ok, bot, reg, _db = await _run(monkeypatch, latest_msg_id=77, result=("s", "", "—", 0))
    assert not ok
    bot.edit_message_text.assert_not_awaited()
    reg.assert_not_awaited()


async def test_hde_lookup_error_after_fetch_returns_false_without_raising(monkeypatch):
    """Fix round 1, finding 2: a background task must never crash silently —
    any error past the initial HDE fetch (here: db.get_topic) is caught."""
    ok, bot, reg, _db = await _run(
        monkeypatch, latest_msg_id=77,
        get_topic=AsyncMock(side_effect=RuntimeError("boom")))
    assert ok is False


async def test_v2_single_message_instead_of_three(monkeypatch):
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with (
        patch("bot.handlers.ai_feedback.register_feedback_pending", new=AsyncMock()),
        patch("bot.topic_manager.db") as db,
    ):
        db.update_topic = AsyncMock()
        db.get_topic = AsyncMock(return_value=None)
        ok = await topic_history.post_suggestion_messages(
            bot, topic_id=1, ticket_id="T", suit_line="Атол не печатает",
            client_line="Перезагрузите кассу.", memo_line="—", confidence_pct=90,
            all_posts=[], info=SimpleNamespace(client_id=1), ticket_title="t", anchor="5",
        )
    assert ok
    assert bot.send_message.await_count == 1
    assert "Атол не печатает" in bot.send_message.await_args.kwargs["text"]


async def test_v2_strips_old_button_and_tracks_new_one(monkeypatch):
    """Fix round 1, finding 1: every v2 message carrying the draft keyboard
    becomes the topic's suggest_button_msg_id; the previous one is stripped
    so a stale 📤 can never send a newer draft."""
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    bot.edit_message_reply_markup = AsyncMock()
    with (
        patch("bot.handlers.ai_feedback.register_feedback_pending", new=AsyncMock()),
        patch("bot.topic_manager.db") as db,
    ):
        db.update_topic = AsyncMock()
        db.get_topic = AsyncMock(return_value=SimpleNamespace(suggest_button_msg_id=55))
        ok = await topic_history.post_suggestion_messages(
            bot, topic_id=1, ticket_id="T", suit_line="Атол не печатает",
            client_line="Перезагрузите кассу.", memo_line="—", confidence_pct=90,
            all_posts=[], info=SimpleNamespace(client_id=1), ticket_title="t", anchor="5",
        )
    assert ok
    assert bot.edit_message_reply_markup.await_args.kwargs["message_id"] == 55
    assert any(
        c.kwargs.get("suggest_button_msg_id") == 999
        for c in db.update_topic.await_args_list
    )


async def test_v2_no_action_marks_summary_sent(monkeypatch):
    """Fix round 1, finding 3: NO_ACTION on a first message must still stamp
    ai_summary_sent_at, or retry_missing_ai_summaries regenerates it forever."""
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with (
        patch("bot.handlers.ai_feedback.register_feedback_pending", new=AsyncMock()),
        patch("bot.topic_manager.db") as db,
    ):
        db.update_topic = AsyncMock()
        ok = await topic_history.post_suggestion_messages(
            bot, topic_id=1, ticket_id="T", suit_line="Атол не печатает",
            client_line="", memo_line="—", confidence_pct=90,
            all_posts=[], info=SimpleNamespace(client_id=1), ticket_title="t", anchor="5",
        )
    assert ok
    bot.send_message.assert_not_awaited()
    assert any(
        "ai_summary_sent_at" in c.kwargs for c in db.update_topic.await_args_list
    )
