"""Inline feedback buttons for AI summaries: 👍 good / ✏️ correct / 👎 bad."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..db import (
    delete_ai_feedback_pending,
    get_ai_feedback_pending,
    save_ai_feedback_pending,
)
from ..knowledge.indexer import index_knowledge_item

logger = logging.getLogger(__name__)
router = Router()

_TTL_HOURS = 24

# Slash-commands must never be swallowed by capture_correction: this router
# is registered before commands_router, so without this guard a pending
# correction in a topic would hijack /autofill, /note, etc.
_NOT_COMMAND = ~F.text.startswith("/")


def make_ai_feedback_keyboard() -> InlineKeyboardMarkup:
    """Legacy keyboard — kept for backwards compatibility."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍", callback_data="ai:good"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data="ai:edit"),
        InlineKeyboardButton(text="👎", callback_data="ai:bad"),
    ]])


def suit_feedback_kb() -> InlineKeyboardMarkup:
    """Keyboard for the Суть message."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Верно", callback_data="suit:good"),
        InlineKeyboardButton(text="👎 Неверно", callback_data="suit:bad"),
    ]])


def answer_feedback_kb() -> InlineKeyboardMarkup:
    """Keyboard for the Предложенный ответ message."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍", callback_data="ai:good"),
            InlineKeyboardButton(text="✏️", callback_data="ai:edit"),
            InlineKeyboardButton(text="👎", callback_data="ai:bad"),
        ],
        [
            InlineKeyboardButton(text="📤 Ответить клиенту", callback_data="ai:send_post"),
            InlineKeyboardButton(text="💬 Комментарий", callback_data="ai:send_comment"),
        ],
    ])


def memo_feedback_kb() -> InlineKeyboardMarkup:
    """Keyboard for the Памятка message."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Полезно", callback_data="memo:good"),
        InlineKeyboardButton(text="👎 Бесполезно", callback_data="memo:bad"),
    ]])


async def register_feedback_pending(
    topic_id: int,
    ticket_id: str,
    history: str,
    title: str,
    answer_text: str = "",
    ai_full_text: str = "",
) -> None:
    """Store pending feedback state so correction handler can pick it up."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_TTL_HOURS)
    ).isoformat()
    await save_ai_feedback_pending(
        topic_id, ticket_id, history, title, expires_at, answer_text, ai_full_text
    )


# ── Callbacks ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ai:good")
async def cb_ai_good(callback: CallbackQuery) -> None:
    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    await callback.answer("✅ Сохранено в базу знаний", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)
    if pending is None:
        return
    await delete_ai_feedback_pending(topic_id)
    content = f"Тема: {pending['title']}\n\n{pending['history']}"
    await index_knowledge_item(
        source="feedback",
        content=content,
        ticket_id=pending["ticket_id"],
        title=pending["title"],
        quality="good",
    )
    logger.info("Saved good example for ticket %s", pending["ticket_id"])
    # Save for prompt optimizer
    try:
        from .. import db as _db_module
        await _db_module.save_optimization_sample(
            ticket_id=pending.get("ticket_id", ""),
            title=pending.get("title", ""),
            history=pending.get("history", ""),
            ai_answer=pending.get("ai_full_text") or pending.get("answer_text", ""),
            op_answer=None,
            outcome="accepted",
        )
    except Exception as exc:
        logger.warning("ai_feedback: optimization sample save failed: %s", exc)
    # Update wiki article (non-fatal)
    try:
        from ..wiki.builder import build_or_update_wiki_article
        await build_or_update_wiki_article(
            title=pending["title"],
            content=content,
            ticket_id=pending["ticket_id"],
        )
    except Exception as exc:
        logger.warning("Wiki update failed after 👍: %s", exc)


@router.callback_query(F.data == "ai:bad")
async def cb_ai_bad(callback: CallbackQuery) -> None:
    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)
    await delete_ai_feedback_pending(topic_id)
    logger.info("Marked summary as bad for topic %d", topic_id)
    # Save for prompt optimizer
    if pending:
        try:
            from .. import db as _db_module
            await _db_module.save_optimization_sample(
                ticket_id=pending.get("ticket_id", ""),
                title=pending.get("title", ""),
                history=pending.get("history", ""),
                ai_answer=pending.get("answer_text", ""),
                op_answer=None,
                outcome="rejected",
            )
        except Exception as exc:
            logger.warning("ai_feedback: optimization sample save failed: %s", exc)


@router.callback_query(F.data == "ai:edit")
async def cb_ai_edit(callback: CallbackQuery) -> None:
    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)
    if pending is None:
        return
    await callback.message.answer(
        "✏️ <b>Введи правильный ответ клиенту</b> — я сохраню его как пример.\n"
        "<i>Следующее сообщение в этом топике будет сохранено.</i>",
        parse_mode="HTML",
    )


# ── Correction capture ───────────────────────────────────────────────────────

class _HasPendingCorrection(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.message_thread_id is None:
            return False
        if message.from_user is None or message.from_user.is_bot:
            return False
        pending = await get_ai_feedback_pending(message.message_thread_id)
        return pending is not None


@router.callback_query(F.data == "suit:good")
async def cb_suit_good(callback: CallbackQuery) -> None:
    await callback.answer("👍 Диагноз отмечен верным", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data == "suit:bad")
async def cb_suit_bad(callback: CallbackQuery) -> None:
    await callback.answer("👎 Отмечено", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data == "memo:good")
async def cb_memo_good(callback: CallbackQuery) -> None:
    await callback.answer("👍 Отмечено", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data == "memo:bad")
async def cb_memo_bad(callback: CallbackQuery) -> None:
    await callback.answer("👎 Отмечено", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data.in_({"ai:send_post", "ai:send_comment"}))
async def cb_send_to_hde(callback: CallbackQuery) -> None:
    from ..hde_api import HDEApiClient, HDEApiError

    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer("⚠️ Не удалось определить топик", show_alert=True)
        return

    topic_id = callback.message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    if not pending:
        await callback.answer("⚠️ Данные устарели (24ч TTL)", show_alert=True)
        return

    answer_text = pending.get("answer_text", "")
    if not answer_text:
        await callback.answer("⚠️ Текст ответа не найден", show_alert=True)
        return

    try:
        client = HDEApiClient()
        if callback.data == "ai:send_post":
            await client.add_post(pending["ticket_id"], answer_text)
            label = "клиенту"
        else:
            await client.add_comment(pending["ticket_id"], answer_text)
            label = "как комментарий"
    except HDEApiError as exc:
        await callback.answer(f"❌ Ошибка HDE: {exc}", show_alert=True)
        return

    await callback.answer(f"✅ Отправлено {label}", show_alert=False)
    try:
        original = callback.message.html_text or callback.message.text or ""
        await callback.message.edit_text(
            original + f"\n\n<i>✅ Отправлено {label}</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as exc:
            logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)
    logger.info("Sent AI answer to HDE ticket %s (%s)", pending["ticket_id"], label)
    # Save for prompt optimizer
    try:
        from .. import db as _db_module
        await _db_module.save_optimization_sample(
            ticket_id=pending.get("ticket_id", ""),
            title=pending.get("title", ""),
            history=pending.get("history", ""),
            ai_answer=pending.get("ai_full_text") or pending.get("answer_text", ""),
            op_answer=None,
            outcome="sent",
        )
    except Exception as exc:
        logger.warning("ai_feedback: optimization sample save failed: %s", exc)


@router.message(_HasPendingCorrection(), F.text.is_not(None), _NOT_COMMAND)
async def capture_correction(message: Message) -> None:
    topic_id = message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    if pending is None:
        return
    await delete_ai_feedback_pending(topic_id)
    correction_text = message.text.strip()
    content = (
        f"Тема: {pending['title']}\n\n"
        f"{pending['history']}\n\n"
        f"Правильный ответ: {correction_text}"
    )
    await index_knowledge_item(
        source="feedback",
        content=content,
        ticket_id=pending["ticket_id"],
        title=pending["title"],
        quality="corrected",
    )
    # Save for prompt optimizer — corrected outcome with operator's real text
    try:
        from .. import db as _db_module
        await _db_module.save_optimization_sample(
            ticket_id=pending.get("ticket_id", ""),
            title=pending.get("title", ""),
            history=pending.get("history", ""),
            ai_answer=pending.get("ai_full_text") or pending.get("answer_text", ""),
            op_answer=correction_text,
            outcome="corrected",
        )
    except Exception as exc:
        logger.warning("ai_feedback: optimization sample save failed: %s", exc)
    # Update wiki article (non-fatal)
    try:
        from ..wiki.builder import build_or_update_wiki_article
        await build_or_update_wiki_article(
            title=pending["title"],
            content=content,
            ticket_id=pending["ticket_id"],
        )
    except Exception as exc:
        logger.warning("Wiki update failed after correction: %s", exc)
    await message.answer("✅ <b>Сохранено как исправленный пример</b>", parse_mode="HTML")
