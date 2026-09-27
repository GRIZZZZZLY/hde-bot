"""Ticket history posting and AI summary delivery to topics.

Mechanically extracted from bot.topic_manager (pure move, no behavior change).
Shared state and test patch-points (config, db, utcnow, _post_client_history,
_generate_summary_with_retry, format_ticket_history, ...) are accessed
late-bound through the bot.topic_manager module object so monkeypatches on
bot.topic_manager keep working.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .time_utils import to_storage

logger = logging.getLogger(__name__)


async def post_suggestion_messages(
    bot: Bot,
    *,
    topic_id: int,
    ticket_id: str,
    suit_line: str,
    client_line: str,
    memo_line: str,
    confidence_pct: int,
    all_posts,
    info,
    ticket_title: str,
    anchor: str | None,
    trigger_source: str = "first",
) -> bool:
    """Post the Суть/Ответ/Памятка triple with feedback keyboards and register
    the pending-feedback state. Returns True on success (shared by the first
    auto-suggestion, the missing-summary retry job and the 💡 button)."""
    from . import topic_manager as _tm
    from .ai_summary import _build_history_text
    from .handlers.ai_feedback import suit_feedback_kb, answer_feedback_kb, memo_feedback_kb, register_feedback_pending
    from html import escape as _html_escape

    # Кнопка «💡 Предложить ответ» отдаёт только ответ клиенту — без Сути и Памятки.
    answer_only = trigger_source == "button"
    try:
        if _tm.config.agent_voice_v2_enabled:
            from .formatter import format_draft_block
            from .handlers.ai_feedback import draft_kb
            if not client_line and (memo_line or "").strip() in ("", "—"):
                await _tm.db.update_topic(ticket_id, ai_summary_sent_at=to_storage(_tm.utcnow()))
                return True                                   # NO_ACTION — молчим
            record = await _tm.db.get_topic(ticket_id)
            await _tm._strip_prev_suggest_button(
                bot, record.suggest_button_msg_id if record else None
            )
            sent = await bot.send_message(
                chat_id=_tm.config.group_chat_id,
                message_thread_id=topic_id,
                text=format_draft_block(
                    client_line, memo_line,
                    suit=suit_line if trigger_source == "first" else None,
                    separator=False,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=True,
                reply_markup=draft_kb() if client_line else None,
            )
            if client_line:
                await _tm.db.update_topic(ticket_id, suggest_button_msg_id=sent.message_id)
        else:
            if not answer_only:
                suit_label = (
                    f"🧠 <b>Суть ({confidence_pct}%):</b>"
                    if confidence_pct >= 40
                    else "🧠 <b>Суть:</b>"
                )
                await bot.send_message(
                    chat_id=_tm.config.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"{suit_label} {_html_escape(suit_line)}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    disable_notification=True,
                    reply_markup=suit_feedback_kb(),
                )
            if client_line:
                await bot.send_message(
                    chat_id=_tm.config.group_chat_id,
                    message_thread_id=topic_id,
                    text=(
                        f"💬 <b>Ответ клиенту:</b>\n"
                        f"<i>«{_html_escape(client_line)}»</i>"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    disable_notification=True,
                    reply_markup=answer_feedback_kb(),
                )
            if memo_line and not answer_only:
                await bot.send_message(
                    chat_id=_tm.config.group_chat_id,
                    message_thread_id=topic_id,
                    text=(
                        f"📋 <b>Памятка:</b>\n"
                        f"{_html_escape(memo_line)}"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    disable_notification=True,
                    reply_markup=memo_feedback_kb(),
                )
        plain_history = _build_history_text(all_posts, info)
        ai_full_text = (
            f"Суть: {suit_line}\n"
            f"Клиенту: {client_line}\n"
            f"Памятка: {memo_line or '—'}"
        )
        await register_feedback_pending(
            topic_id=topic_id,
            ticket_id=ticket_id,
            history=plain_history,
            title=ticket_title,
            answer_text=client_line,
            ai_full_text=ai_full_text,
            trigger_source=trigger_source,
            context_until_post_id=anchor,
        )
        await _tm.db.update_topic(ticket_id, ai_summary_sent_at=to_storage(_tm.utcnow()))
        return True
    except TelegramAPIError as exc:
        logger.warning("Failed to post AI summary to topic %d: %s", topic_id, exc)
        return False


_TG_LIMIT = 4096


async def append_draft_to_reply(
    bot: Bot,
    *,
    ticket_id: str,
    topic_id: int,
    message_id: int,
    reply_html: str,
    ticket_title: str,
) -> bool:
    """Черновик на ответ клиента: дописать в то же сообщение (spec §6).

    Кнопки и pending — только если это сообщение всё ещё последнее: иначе
    черновик на старый ответ мог бы уйти клиенту кнопкой 📤.
    """
    from . import topic_manager as _tm
    from .ai_summary import _build_history_text
    from .formatter import format_draft_block
    from .handlers.ai_feedback import draft_kb, register_feedback_pending
    from .hde_api import HDEApiClient, HDEApiError

    try:
        client = HDEApiClient()
        info = await client.get_ticket_info(ticket_id)
        posts = await client.get_ticket_posts(ticket_id)
        try:
            comments = await client.get_ticket_comments(ticket_id)
        except HDEApiError:
            comments = []
    except Exception as exc:
        logger.warning("reply draft: HDE fetch failed for ticket %s: %s", ticket_id, exc)
        return False

    try:
        all_posts = sorted(posts + comments, key=lambda p: p.date_created)
        anchor = str(max((p.post_id for p in all_posts), default="")) or None
        result = await _tm._generate_summary_with_retry(
            all_posts, info, ticket_title=ticket_title, ticket_id=ticket_id,
            company_id="", topic_id=topic_id, trigger_source="reply",
        )
        if result is None:
            return False
        suit_line, client_line, memo_line, _conf = result
        if not client_line and (memo_line or "").strip() in ("", "—"):
            return False                                           # NO_ACTION

        latest = await _tm.db.get_topic(ticket_id)
        is_latest = latest is not None and latest.suggest_button_msg_id == message_id
        markup = draft_kb() if (is_latest and client_line) else None
        block = format_draft_block(client_line, memo_line)
        try:
            if len(reply_html) + len(block) > _TG_LIMIT:
                sent = await bot.send_message(
                    chat_id=_tm.config.group_chat_id, message_thread_id=topic_id,
                    text=block.lstrip("\n─"), parse_mode="HTML",
                    disable_web_page_preview=True, disable_notification=True,
                    reply_markup=markup,
                )
                if markup is not None:
                    # Кнопка была на сообщении клиента (is_latest) — теперь она
                    # переехала в это новое сообщение, старую снимаем.
                    await _tm._strip_prev_suggest_button(bot, message_id)
                    await _tm.db.update_topic(ticket_id, suggest_button_msg_id=sent.message_id)
            else:
                await bot.edit_message_text(
                    chat_id=_tm.config.group_chat_id, message_id=message_id,
                    text=reply_html + block, parse_mode="HTML",
                    disable_web_page_preview=True, reply_markup=markup,
                )
        except TelegramAPIError as exc:
            logger.warning("reply draft: delivery failed for topic %d: %s", topic_id, exc)
            return False

        if markup is not None:
            await register_feedback_pending(
                topic_id=topic_id, ticket_id=ticket_id,
                history=_build_history_text(all_posts, info), title=ticket_title,
                answer_text=client_line,
                ai_full_text=f"Суть: {suit_line}\nКлиенту: {client_line}\nПамятка: {memo_line or '—'}",
                trigger_source="reply", context_until_post_id=anchor,
            )
        return True
    except Exception:
        logger.exception("reply draft failed for ticket %s", ticket_id)
        return False


async def retry_missing_ai_summaries(bot: Bot) -> int:
    """Find topics that never got an AI summary and retry. Returns number of summaries sent."""
    from . import topic_manager as _tm
    from .hde_api import HDEApiClient, HDEApiError

    records = await _tm.db.list_topics_missing_summary()
    sent = 0
    try:
        client = HDEApiClient()
    except HDEApiError as exc:
        for record in records:
            logger.warning("retry_missing_ai_summaries: can't fetch ticket %s: %s", record.ticket_id, exc)
        return 0
    except Exception as exc:
        for record in records:
            logger.error("retry_missing_ai_summaries: unexpected error for ticket %s: %s", record.ticket_id, exc)
        return 0

    for record in records:
        ticket_id = record.ticket_id
        topic_id = record.topic_id
        try:
            info = await client.get_ticket_info(ticket_id)
            posts = await client.get_ticket_posts(ticket_id)
            try:
                comments = await client.get_ticket_comments(ticket_id)
            except HDEApiError:
                comments = []
        except HDEApiError as exc:
            logger.warning("retry_missing_ai_summaries: can't fetch ticket %s: %s", ticket_id, exc)
            continue
        except Exception as exc:
            logger.error("retry_missing_ai_summaries: unexpected error for ticket %s: %s", ticket_id, exc)
            continue

        all_posts = sorted(posts + comments, key=lambda p: p.date_created)
        anchor = str(max((p.post_id for p in all_posts), default="")) or None
        result = await _tm._generate_summary_with_retry(
            all_posts, info,
            ticket_title=record.ticket_name or "",
            ticket_id=ticket_id,
            company_id="",
            topic_id=topic_id,
        )
        if result is None:
            continue

        suit_line, client_line, memo_line, confidence_pct = result
        ok = await post_suggestion_messages(
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
        )
        if ok:
            sent += 1

    return sent


async def _post_ticket_history(
    bot: Bot,
    ticket_id: str,
    topic_id: int,
    ticket_title: str = "",
    company_id: str = "",
) -> None:
    """Fetch conversation history from HDE and post it to the topic (oldest→newest).

    AI generation starts immediately in background; summary is posted after history.
    """
    from . import topic_manager as _tm
    if not _tm.config.has_hde_api_credentials():
        return
    from .hde_api import HDEApiClient, HDEApiError
    try:
        client = HDEApiClient()
        info = await client.get_ticket_info(ticket_id)
        posts = await client.get_ticket_posts(ticket_id)
        try:
            comments = await client.get_ticket_comments(ticket_id)
        except HDEApiError:
            comments = []
    except HDEApiError as exc:
        logger.warning("Could not fetch history for ticket %s: %s", ticket_id, exc)
        return
    except Exception as exc:
        logger.error("Unexpected error fetching history for ticket %s: %s", ticket_id, exc)
        return

    # Merge posts and comments, sort by date_created ascending
    all_posts = sorted(posts + comments, key=lambda p: p.date_created)
    anchor = str(max((p.post_id for p in all_posts), default="")) or None

    await _tm._post_client_history(bot, topic_id, ticket_id, client=client, info=info)

    if not all_posts:
        logger.info("No posts for ticket %s, skipping history+summary", ticket_id)
        return

    # Start AI generation immediately — runs in parallel with history posting
    from .ai_summary import generate_ticket_summary, _build_history_text
    gen_task = asyncio.create_task(
        _tm._generate_summary_with_retry(
            all_posts, info,
            ticket_title=ticket_title,
            ticket_id=ticket_id,
            company_id=company_id,
            topic_id=topic_id,
        )
    )

    # Post history while generation runs in background
    if _tm.config.ticket_history_post_enabled:
        messages = _tm.format_ticket_history(all_posts, info)
        for text in messages:
            try:
                await bot.send_message(
                    chat_id=_tm.config.group_chat_id,
                    message_thread_id=topic_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    disable_notification=True,
                )
            except TelegramAPIError as exc:
                logger.warning("Failed to post history message to topic %d: %s", topic_id, exc)
                break

    # Await generation result (was running during history posting)
    result = await gen_task
    if result is None:
        # Подсказки нет (выключена или LLM не ответила) — это НЕ повод пропускать
        # автозаполнение полей: приоритет и тип тикета от текста подсказки не
        # зависят, а раньше они молча не заполнялись при любом сбое генерации.
        logger.info("AI summary not generated for ticket %s", ticket_id)
        if _tm.config.agent_voice_v2_enabled:
            from .handlers.ai_feedback import suggest_button_kb
            try:
                await bot.send_message(
                    chat_id=_tm.config.group_chat_id, message_thread_id=topic_id,
                    text="💡 Черновик по первому сообщению — по кнопке",
                    disable_notification=True, reply_markup=suggest_button_kb(),
                )
            except TelegramAPIError as exc:
                logger.warning("first-message suggest button failed for topic %d: %s", topic_id, exc)
    else:
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
            ticket_title=ticket_title,
            anchor=anchor,
        )

    try:
        from .ticket_fields import apply_ticket_fields
        autofill_history = _build_history_text(all_posts, info)
        await apply_ticket_fields(
            bot, ticket_id, topic_id, autofill_history,
            ticket_title=ticket_title, posts=all_posts,
        )
    except Exception as exc:
        logger.warning("Ticket field auto-fill failed for %s: %s", ticket_id, exc)
