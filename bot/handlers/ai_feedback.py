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
    get_open_suggestion_by_topic,
    record_suggestion,
    record_suggestion_event,
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


def suggest_button_kb() -> InlineKeyboardMarkup:
    """Keyboard attached to client-reply messages in the topic (Phase 3)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💡 Предложить ответ", callback_data="ai:suggest"),
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


def kb_candidate_kb(candidate_id: int) -> InlineKeyboardMarkup:
    """Решение по кандидату в базу знаний из ночной сверки."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ В базу", callback_data=f"kbc:add:{candidate_id}"),
        InlineKeyboardButton(text="✖️ Мимо", callback_data=f"kbc:skip:{candidate_id}"),
    ]])


def kb_conflict_kb(candidate_id: int) -> InlineKeyboardMarkup:
    """Выбор стороны в противоречии: новое правило против уже лежащего в базе.

    Не «добавить / мимо»: «мимо» здесь означало бы оставить в базе строку,
    которую оператор своим ответом только что опроверг."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Новое верно",
                             callback_data=f"kbc:new:{candidate_id}"),
        InlineKeyboardButton(text="↩️ Старое верно",
                             callback_data=f"kbc:old:{candidate_id}"),
    ]])


@router.callback_query(F.data.startswith("kbc:"))
async def cb_kb_candidate(callback: CallbackQuery) -> None:
    from ..agent.kb_candidates import apply_kb_candidate, resolve_kb_conflict_decision

    _, action, raw_id = callback.data.split(":", 2)
    if action in ("new", "old"):
        row = await resolve_kb_conflict_decision(int(raw_id), keep_new=(action == "new"))
        answer = (
            "✅ Новое правило в базе, старое снято" if action == "new"
            else "Оставили как было"
        )
    else:
        row = await apply_kb_candidate(int(raw_id), add=(action == "add"))
        answer = "✅ Добавлено в базу знаний" if action == "add" else "Пропущено"
    if row is None:
        await callback.answer("Уже обработано", show_alert=False)
    else:
        await callback.answer(answer, show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("kb candidate: reply markup cleanup failed: %s", exc)


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
    *,
    trigger_source: str = "first",
    context_until_post_id: str | None = None,
    client_id: str | None = None,
) -> None:
    """Store pending feedback state so correction handler can pick it up, and
    (non-fatally) record the suggestion row for tracing."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_TTL_HOURS)
    ).isoformat()
    await save_ai_feedback_pending(
        topic_id, ticket_id, history, title, expires_at, answer_text, ai_full_text
    )
    try:
        from ..config import config
        from ..ai_summary import prompt_version_tag
        await record_suggestion(
            ticket_id=ticket_id,
            topic_id=topic_id,
            trigger_source=trigger_source,
            context_until_post_id=context_until_post_id,
            pipeline_version=config.agent_pipeline_version,
            prompt_version=prompt_version_tag(),
            title=title,
            history=history,
            ai_answer=answer_text,
            ai_full_text=ai_full_text,
            client_id=client_id,
        )
    except Exception as exc:
        logger.warning("ai_feedback: suggestion record failed: %s", exc)


async def _record_event(
    topic_id: int,
    event_type: str,
    *,
    payload: str | None = None,
    hde_post_id: str | None = None,
) -> None:
    """Non-fatal: attach an operator-action event to the topic's latest suggestion."""
    try:
        suggestion = await get_open_suggestion_by_topic(topic_id)
        if suggestion is None:
            return
        await record_suggestion_event(
            suggestion["id"], event_type, payload=payload, hde_post_id=hde_post_id
        )
    except Exception as exc:
        logger.warning("ai_feedback: event record failed (%s): %s", event_type, exc)


async def _record_event_from_callback(callback: CallbackQuery, event_type: str) -> None:
    """Достаёт topic_id из карточки и пишет событие (non-fatal)."""
    msg = callback.message
    if msg is not None and getattr(msg, "message_thread_id", None) is not None:
        await _record_event(msg.message_thread_id, event_type)


# ── Callbacks ────────────────────────────────────────────────────────────────

# Топики, в которых прямо сейчас идёт генерация по кнопке — защита от двойного
# клика (идемпотентность в БД спасает от дублей записи, но не от двойной
# генерации и двойного поста).
_suggest_in_flight: set[int] = set()


@router.callback_query(F.data == "ai:suggest")
async def cb_ai_suggest(callback: CallbackQuery) -> None:
    """Phase 3: кнопка «💡 Предложить ответ» — свежая история из HDE →
    пайплайн фазы 1 → новая тройка Суть/Ответ/Памятка в топик."""
    from .. import db as _db_module

    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    if topic_id in _suggest_in_flight:
        await callback.answer("⏳ Уже генерирую подсказку", show_alert=False)
        return
    record = await _db_module.get_topic_by_topic_id(topic_id)
    if record is None:
        await callback.answer("⚠️ Тикет для этого топика не найден", show_alert=True)
        return
    _suggest_in_flight.add(topic_id)
    await callback.answer("💡 Генерирую подсказку…", show_alert=False)
    try:
        await _generate_and_post_suggestion(callback.bot, record)
    finally:
        _suggest_in_flight.discard(topic_id)


async def _generate_and_post_suggestion(bot, record) -> None:
    from .. import topic_manager as _tm
    from ..hde_api import HDEApiClient, HDEApiError
    from ..topic_history import post_suggestion_messages

    ticket_id = record.ticket_id
    topic_id = record.topic_id

    async def _notify_failure() -> None:
        try:
            await bot.send_message(
                chat_id=_tm.config.group_chat_id,
                message_thread_id=topic_id,
                text="⚠️ Не удалось сгенерировать подсказку — попробуй ещё раз.",
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning("ai:suggest: failure notice failed for topic %d: %s", topic_id, exc)

    try:
        client = HDEApiClient()
        info = await client.get_ticket_info(ticket_id)
        posts = await client.get_ticket_posts(ticket_id)
        try:
            comments = await client.get_ticket_comments(ticket_id)
        except HDEApiError:
            comments = []
    except Exception as exc:
        logger.warning("ai:suggest: HDE fetch failed for ticket %s: %s", ticket_id, exc)
        await _notify_failure()
        return

    all_posts = sorted(posts + comments, key=lambda p: p.date_created)
    anchor = str(max((p.post_id for p in all_posts), default="")) or None

    result = await _tm._generate_summary_with_retry(
        all_posts, info,
        ticket_title=record.ticket_name or "",
        ticket_id=ticket_id,
        company_id="",
        topic_id=topic_id,
        trigger_source="button",
    )
    if result is None:
        await _notify_failure()
        return

    suit_line, client_line, memo_line, confidence_pct = result
    await post_suggestion_messages(
        bot,
        topic_id=topic_id,
        ticket_id=ticket_id,
        suit_line=suit_line,
        client_line=client_line,
        memo_line=memo_line,
        confidence_pct=confidence_pct,
        all_posts=all_posts,
        info=info,
        ticket_title=record.ticket_name or "",
        anchor=anchor,
        trigger_source="button",
    )


@router.callback_query(F.data == "ai:good")
async def cb_ai_good(callback: CallbackQuery) -> None:
    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    await _record_event(topic_id, "approved")
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
    await _record_event(topic_id, "rejected")
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
    await _record_event(topic_id, "edit_started")
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
    await _record_event_from_callback(callback, "suit_good")
    await callback.answer("👍 Диагноз отмечен верным", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data == "suit:bad")
async def cb_suit_bad(callback: CallbackQuery) -> None:
    await _record_event_from_callback(callback, "suit_bad")
    await callback.answer("👎 Отмечено", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data == "memo:good")
async def cb_memo_good(callback: CallbackQuery) -> None:
    await _record_event_from_callback(callback, "memo_good")
    await callback.answer("👍 Отмечено", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("ai_feedback: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data == "memo:bad")
async def cb_memo_bad(callback: CallbackQuery) -> None:
    await _record_event_from_callback(callback, "memo_bad")
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

    await _record_event(topic_id, "send_requested")
    try:
        client = HDEApiClient()
        if callback.data == "ai:send_post":
            await client.add_post(pending["ticket_id"], answer_text)
            label = "клиенту"
        else:
            await client.add_comment(pending["ticket_id"], answer_text)
            label = "как комментарий"
    except HDEApiError as exc:
        await _record_event(topic_id, "send_failed", payload=str(exc))
        await callback.answer(f"❌ Ошибка HDE: {exc}", show_alert=True)
        return

    await callback.answer(f"✅ Отправлено {label}", show_alert=False)
    await _record_event(topic_id, "sent", payload=answer_text)
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
    await _record_event(topic_id, "edited", payload=correction_text)
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
