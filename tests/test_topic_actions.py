from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.handlers.topic_actions as ta
from bot.operator_replies import OperatorReplyError

ENGINEER = 123456789          # in config.operator_telegram_user_ids (conftest)
CHAT = -100123456789


def make_msg(*, text=None, user_id=ENGINEER, is_bot=False, thread=9001, media_group_id=None,
             photo=None, document=None, audio=None, voice=None, video=None, animation=None, message_id=10):
    msg = MagicMock()
    msg.message_id = message_id
    msg.message_thread_id = thread
    msg.chat = SimpleNamespace(id=CHAT)
    msg.from_user = SimpleNamespace(id=user_id, is_bot=is_bot)
    msg.text, msg.caption = text, None
    msg.photo, msg.document, msg.audio, msg.voice = photo, document, audio, voice
    msg.video, msg.animation, msg.video_note = video, animation, None
    msg.media_group_id = media_group_id
    msg.reply = AsyncMock()
    return msg


@pytest.fixture(autouse=True)
def _clean_state():
    ta._seen_albums.clear()
    ta._in_flight.clear()


@pytest.mark.parametrize("kwargs,expected", [
    (dict(text="Перезагрузите кассу"), True),
    (dict(photo=[object()]), True),
    (dict(document=SimpleNamespace(mime_type="application/pdf", file_name="act.pdf")), True),
    (dict(text="/send hi"), False),
    (dict(text="hi", user_id=555), False),                     # not an engineer
    (dict(text="hi", is_bot=True), False),
    (dict(voice=SimpleNamespace(file_id="v")), False),         # voice → call note
    (dict(audio=SimpleNamespace(file_id="a", file_name="call.mp3", mime_type="audio/mpeg")), False),
    (dict(document=SimpleNamespace(file_id="d", mime_type="audio/wav", file_name="call.wav")), False),
])
def test_wants_question(kwargs, expected):
    assert ta.wants_question(make_msg(**kwargs)) is expected


def test_topic_router_comes_before_media_router():
    import bot.handlers.commands as cmds
    from bot.handlers.media_commands import router as media_router
    subs = cmds.router.sub_routers
    assert subs.index(ta.router) < subs.index(media_router)


async def test_question_is_a_reply(monkeypatch):
    monkeypatch.setattr(ta, "cache_incoming_topic_media", AsyncMock())
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock())
    msg = make_msg(text="Перезагрузите кассу")
    await ta.ask_where_to_send(msg)
    msg.reply.assert_awaited_once()
    assert msg.reply.await_args.args[0] == "Куда отправить?"


async def test_no_question_outside_active_ticket(monkeypatch):
    cache = AsyncMock()
    monkeypatch.setattr(ta, "cache_incoming_topic_media", cache)
    monkeypatch.setattr(ta, "get_operator_topic_context",
                        AsyncMock(side_effect=OperatorReplyError("Topic не активен для ответа")))
    msg = make_msg(text="hi")
    await ta.ask_where_to_send(msg)
    msg.reply.assert_not_awaited()
    cache.assert_awaited_once()          # media cache still filled


async def test_album_gets_one_question(monkeypatch):
    cache = AsyncMock()
    monkeypatch.setattr(ta, "cache_incoming_topic_media", cache)
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock())
    msgs = [make_msg(photo=[object()], media_group_id="alb1", message_id=i) for i in (1, 2, 3)]
    for m in msgs:
        await ta.ask_where_to_send(m)
    assert sum(m.reply.await_count for m in msgs) == 1
    assert cache.await_count == 3


def make_callback(data, *, original=None, user_id=ENGINEER, prompt_id=50):
    cb = MagicMock()
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id)
    cb.answer = AsyncMock()
    cb.bot = MagicMock()
    prompt = MagicMock()
    prompt.message_id = prompt_id
    prompt.message_thread_id = 9001
    prompt.chat = SimpleNamespace(id=CHAT)
    prompt.reply_to_message = original
    prompt.edit_text = AsyncMock()
    prompt.edit_reply_markup = AsyncMock()
    prompt.delete = AsyncMock()
    cb.message = prompt
    return cb


async def test_client_button_sends_with_original_id(monkeypatch):
    send = AsyncMock(return_value="✅ sent")
    monkeypatch.setattr(ta, "send_public_reply", send)
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock(return_value="CTX"))
    original = make_msg(text="Перезагрузите кассу", message_id=77)
    cb = make_callback("tp:client", original=original)
    await ta.cb_send(cb)
    kw = send.await_args.kwargs
    assert kw["tg_message_id"] == 77 and kw["message"] is cb.message and kw["command_args"] is None
    cb.message.edit_reply_markup.assert_awaited()            # keyboard gone before sending
    text, = cb.message.edit_text.await_args.args
    assert text == "✅ sent"
    assert cb.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "tp:del:77"


async def test_note_button_uses_internal_note(monkeypatch):
    note = AsyncMock(return_value="📝 saved")
    monkeypatch.setattr(ta, "add_internal_note", note)
    monkeypatch.setattr(ta, "send_public_reply", AsyncMock())
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock(return_value="CTX"))
    await ta.cb_send(make_callback("tp:note", original=make_msg(text="x", message_id=5)))
    note.assert_awaited_once()
    ta.send_public_reply.assert_not_awaited()


async def test_double_tap_sends_once(monkeypatch):
    import asyncio
    gate = asyncio.Event()

    async def slow_send(**kwargs):
        await gate.wait()
        return "ok"

    send = AsyncMock(side_effect=slow_send)
    monkeypatch.setattr(ta, "send_public_reply", send)
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock(return_value="CTX"))
    original = make_msg(text="x", message_id=7)
    first = asyncio.create_task(ta.cb_send(make_callback("tp:client", original=original)))
    await asyncio.sleep(0)
    await ta.cb_send(make_callback("tp:client", original=original))
    gate.set()
    await first
    assert send.await_count == 1


async def test_non_engineer_press_sends_nothing(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(ta, "send_public_reply", send)
    monkeypatch.setattr(ta, "get_operator_topic_context",
                        AsyncMock(side_effect=OperatorReplyError("У вас нет прав для этой команды")))
    cb = make_callback("tp:client", original=make_msg(text="x"), user_id=555)
    await ta.cb_send(cb)
    send.assert_not_awaited()
    kwargs = cb.message.edit_text.await_args.kwargs
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "tp:client"   # buttons back
    assert "нет прав" in cb.message.edit_text.await_args.args[0]


async def test_original_deleted(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(ta, "send_public_reply", send)
    cb = make_callback("tp:client", original=None)
    await ta.cb_send(cb)
    send.assert_not_awaited()
    cb.message.delete.assert_awaited_once()


async def test_cancel_deletes_question():
    cb = make_callback("tp:cancel", original=make_msg(text="x"))
    await ta.cb_cancel(cb)
    cb.message.delete.assert_awaited_once()


async def test_delete_needs_confirmation(monkeypatch):
    delete = AsyncMock(return_value="🗑 deleted")
    monkeypatch.setattr(ta, "delete_operator_message", delete)
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock(return_value="CTX"))
    cb = make_callback("tp:del:77")
    await ta.cb_delete(cb)
    delete.assert_not_awaited()
    confirm = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in confirm.inline_keyboard for b in row] == ["tp:delyes:77", "tp:delno:77"]

    cb = make_callback("tp:delyes:77")
    await ta.cb_delete(cb)
    delete.assert_awaited_once_with(context="CTX", telegram_message_id=77)
    assert cb.message.edit_text.await_args.args[0] == "🗑 deleted"


async def test_delete_no_restores_button():
    cb = make_callback("tp:delno:77")
    await ta.cb_delete(cb)
    kb = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "tp:del:77"


async def test_delete_rejects_garbage_id(monkeypatch):
    delete = AsyncMock()
    monkeypatch.setattr(ta, "delete_operator_message", delete)
    await ta.cb_delete(make_callback("tp:delyes:abc"))
    delete.assert_not_awaited()


def test_suggest_keyboard_has_autofill():
    from bot.handlers.ai_feedback import suggest_button_kb
    data = [b.callback_data for row in suggest_button_kb().inline_keyboard for b in row]
    assert data == ["ai:suggest", "tp:autofill"]


async def test_autofill_button_answers_in_topic(monkeypatch):
    import bot.handlers.commands as cmds
    run = AsyncMock(return_value="✅ fields")
    monkeypatch.setattr(cmds, "run_autofill", run, raising=False)
    cb = make_callback("tp:autofill")
    cb.message.answer = AsyncMock()
    await ta.cb_autofill(cb)
    run.assert_awaited_once_with(cb.bot, chat_id=CHAT, topic_id=9001, tg_user_id=ENGINEER)
    cb.message.answer.assert_awaited_once_with("✅ fields", parse_mode="HTML")


async def test_autofill_button_ai_off(monkeypatch, initialized_db):
    import bot.handlers.commands as cmds
    import bot.operators as ops
    monkeypatch.setattr(cmds, "get_operator_topic_context", AsyncMock(return_value=SimpleNamespace(
        record=SimpleNamespace(ticket_id="T1", ticket_name=""), topic_id=9001)))
    monkeypatch.setattr(ops, "ai_enabled_for", lambda **kw: False)
    text = await cmds.run_autofill(MagicMock(), chat_id=CHAT, topic_id=9001, tg_user_id=ENGINEER)
    assert ops.AI_OFF_NOTE in text
