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


def make_ai_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍", callback_data="ai:good"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data="ai:edit"),
        InlineKeyboardButton(text="👎", callback_data="ai:bad"),
    ]])


async def register_feedback_pending(
    topic_id: int,
    ticket_id: str,
    history: str,
    title: str,
) -> None:
    """Store pending feedback state so correction handler can pick it up."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_TTL_HOURS)
    ).isoformat()
    await save_ai_feedback_pending(topic_id, ticket_id, history, title, expires_at)


# ── Callbacks ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ai:good")
async def cb_ai_good(callback: CallbackQuery) -> None:
    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
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


@router.callback_query(F.data == "ai:bad")
async def cb_ai_bad(callback: CallbackQuery) -> None:
    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await delete_ai_feedback_pending(topic_id)
    logger.info("Marked summary as bad for topic %d", topic_id)


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
    except Exception:
        pass
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


@router.message(_HasPendingCorrection(), F.text.is_not(None))
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
    await message.answer("✅ <b>Сохранено как исправленный пример</b>", parse_mode="HTML")
