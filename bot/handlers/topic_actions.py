"""Ticket-topic actions: write as usual, then pick where it goes with a button.

The bot's question is a reply to the engineer's message, so a button press
brings the original back in callback.message.reply_to_message and nothing has
to be stored. Sending goes through the same functions as /send and /note,
keyed by the original message id, so editing that message later edits the
HDE post too (on_edited_message).
"""
import logging
from collections import deque

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import config
from ..operator_replies import (
    OperatorReplyError,
    add_internal_note,
    cache_incoming_topic_media,
    delete_operator_message,
    format_operator_exception,
    get_operator_topic_context,
    send_public_reply,
)
from ..transcription import extract_audio_meta

logger = logging.getLogger(__name__)
router = Router()

ASK_TEXT = "Куда отправить?"

# An album arrives as several messages sharing media_group_id: ask once per album.
_seen_albums: deque[str] = deque(maxlen=200)
# Questions whose send/delete is running: a second tap must not post twice.
_in_flight: set[tuple[int, int]] = set()


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def ask_keyboard() -> InlineKeyboardMarkup:
    return _kb([[("📤 Клиенту", "tp:client"), ("💬 Комментарий", "tp:note"), ("✖️", "tp:cancel")]])


def delete_keyboard(orig_id: int) -> InlineKeyboardMarkup:
    return _kb([[("🗑 Удалить из HDE", f"tp:del:{orig_id}")]])


def confirm_delete_keyboard(orig_id: int) -> InlineKeyboardMarkup:
    return _kb([[("Да, удалить", f"tp:delyes:{orig_id}"), ("Нет", f"tp:delno:{orig_id}")]])


def wants_question(message: Message) -> bool:
    """An engineer's own text/photo/video/file/GIF in a topic; not a command, voice or call recording.

    Anything rejected here falls through to the media router as before
    (voice → call note, audio → transcription, everything → media cache).
    """
    user = message.from_user
    if user is None or user.is_bot or not config.is_operator_allowed(user.id):
        return False
    if (message.text or "").startswith("/"):
        return False
    if not (message.text or message.photo or message.video or message.document or message.animation):
        return False
    return extract_audio_meta(message) is None


@router.message(F.message_thread_id.is_not(None), wants_question)
async def ask_where_to_send(message: Message) -> None:
    await cache_incoming_topic_media(message)   # /send-by-reply and albums read this cache
    if message.media_group_id:
        if message.media_group_id in _seen_albums:
            return
        _seen_albums.append(message.media_group_id)
    try:
        await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
            chat_id=message.chat.id,
        )
    except OperatorReplyError:
        return   # General, a closed ticket or an unknown topic: nowhere to send
    await message.reply(ASK_TEXT, reply_markup=ask_keyboard(), disable_notification=True)


@router.callback_query(F.data.in_({"tp:client", "tp:note"}))
async def cb_send(callback: CallbackQuery) -> None:
    prompt = callback.message
    original = prompt.reply_to_message
    if original is None:
        await callback.answer("Исходное сообщение удалено, отправлять нечего", show_alert=True)
        try:
            await prompt.delete()
        except Exception as exc:
            logger.debug("topic_actions: question cleanup failed: %s", exc)
        return
    key = (prompt.chat.id, prompt.message_id)
    if key in _in_flight:
        await callback.answer()
        return
    _in_flight.add(key)
    try:
        await _send(callback, prompt, original)
    finally:
        _in_flight.discard(key)


async def _send(callback: CallbackQuery, prompt: Message, original: Message) -> None:
    to_client = callback.data == "tp:client"
    # Answer the press now: a send that waits on attachments or HDE can outlive
    # the callback query, and a late answer raises before the result is shown.
    await callback.answer()
    try:
        await prompt.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("topic_actions: keyboard removal failed: %s", exc)
    try:
        context = await get_operator_topic_context(
            telegram_user_id=callback.from_user.id,
            topic_id=prompt.message_thread_id,
            chat_id=prompt.chat.id,
        )
        send = send_public_reply if to_client else add_internal_note
        result = await send(
            bot=callback.bot, context=context, message=prompt,
            command_args=None, tg_message_id=original.message_id,
        )
    except Exception as exc:
        if not isinstance(exc, OperatorReplyError):
            logger.exception("topic_actions: send failed")
        await prompt.edit_text(f"{ASK_TEXT}\n{format_operator_exception(exc)}",
                               parse_mode="HTML", reply_markup=ask_keyboard())
        return
    await prompt.edit_text(result, parse_mode="HTML", reply_markup=delete_keyboard(original.message_id))


@router.callback_query(F.data == "tp:cancel")
async def cb_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception as exc:
        logger.debug("topic_actions: question cleanup failed: %s", exc)


@router.callback_query(F.data.startswith("tp:del"))
async def cb_delete(callback: CallbackQuery) -> None:
    _, action, raw_id = (callback.data.split(":") + ["", ""])[:3]
    if not raw_id.isdigit():
        await callback.answer()
        return
    orig_id = int(raw_id)
    prompt = callback.message
    if action == "del":
        await callback.answer()
        await prompt.edit_reply_markup(reply_markup=confirm_delete_keyboard(orig_id))
        return
    if action == "delno":
        await callback.answer()
        await prompt.edit_reply_markup(reply_markup=delete_keyboard(orig_id))
        return
    if action != "delyes":
        await callback.answer()
        return
    key = (prompt.chat.id, prompt.message_id)
    await callback.answer()   # before HDE: see _send
    if key in _in_flight:
        return
    _in_flight.add(key)
    try:
        await prompt.edit_reply_markup(reply_markup=None)
        context = await get_operator_topic_context(
            telegram_user_id=callback.from_user.id,
            topic_id=prompt.message_thread_id,
            chat_id=prompt.chat.id,
        )
        result = await delete_operator_message(context=context, telegram_message_id=orig_id)
    except Exception as exc:
        if not isinstance(exc, OperatorReplyError):
            logger.exception("topic_actions: delete failed")
        await prompt.edit_text(f"{prompt.html_text}\n{format_operator_exception(exc)}",
                               parse_mode="HTML", reply_markup=delete_keyboard(orig_id))
        return
    finally:
        _in_flight.discard(key)
    await prompt.edit_text(result, parse_mode="HTML")


@router.callback_query(F.data == "tp:autofill")
async def cb_autofill(callback: CallbackQuery) -> None:
    from . import commands   # commands includes this router: import at call time, not at module load

    await callback.answer("Заполняю поля…")
    msg = callback.message
    try:
        result = await commands.run_autofill(
            callback.bot, chat_id=msg.chat.id,
            topic_id=msg.message_thread_id, tg_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if not isinstance(exc, OperatorReplyError):
            logger.exception("topic_actions: autofill failed")
        result = format_operator_exception(exc)
    await msg.answer(result, parse_mode="HTML")
