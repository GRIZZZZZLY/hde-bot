from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

_ticket_locks: dict[str, asyncio.Lock] = {}


def _ticket_lock(ticket_id: str) -> asyncio.Lock:
    if ticket_id not in _ticket_locks:
        _ticket_locks[ticket_id] = asyncio.Lock()
    return _ticket_locks[ticket_id]

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo

from . import db
from .client_media import detect_telegram_media_kind, download_client_attachment
from .config import config
from .formatter import (
    format_assignment_message,
    format_client_history,
    format_client_reply,
    format_pre_sla_alert_general,
    format_pre_sla_alert_topic,
    format_ticket_history,
    format_ticket_renamed,
    format_unassigned_message,
    make_topic_name,
)
from .time_utils import parse_datetime, to_storage, utcnow

logger = logging.getLogger(__name__)

PRIORITY_COLORS = {
    "critical": 0xFB6F5F,
    "high": 0xFFD67E,
    "medium": 0x6FB9F0,
    "low": 0x8EEE98,
}


def _priority_color(priority: str) -> int:
    return PRIORITY_COLORS.get((priority or "").lower(), 0x6FB9F0)


def _display_id(payload: dict) -> str:
    return str(payload.get("unique_id") or payload.get("ticket_id") or "")


def _payload_value(payload: dict, key: str) -> str:
    return str(payload.get(key) or "").strip()


def _build_topic_name(payload: dict) -> str:
    return make_topic_name(
        _display_id(payload),
        _payload_value(payload, "company_name"),
        _payload_value(payload, "ticket_name"),
        _payload_value(payload, "priority") or "medium",
    )


def _topic_metadata(payload: dict) -> dict:
    return {
        "unique_id": _display_id(payload),
        "company_name": _payload_value(payload, "company_name"),
        "ticket_name": _payload_value(payload, "ticket_name"),
        "priority": _payload_value(payload, "priority"),
        "status": _payload_value(payload, "status"),
        "owner_id": _payload_value(payload, "owner_id"),
        "owner_name": _payload_value(payload, "owner_name"),
        "hde_link": _payload_value(payload, "link"),
    }


def _chunked(items: list, size: int) -> list[list]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _effective_owner_match(payload: dict) -> bool:
    return config.matches_owner(
        _payload_value(payload, "owner_id"),
        _payload_value(payload, "owner_name"),
    )


def _parse_minutes(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _relative_date(date_str: str | None) -> str | None:
    """Convert HDE date string to Russian relative label.

    Handles: 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM:SS', 'DD.MM.YYYY HH:MM', 'YYYY-MM-DD'.
    Returns None if unparseable.
    """
    if not date_str:
        return None
    formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d"]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str[:19], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    delta = (utcnow().date() - dt.date()).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    if 2 <= delta <= 4:
        return f"{delta} дня назад"
    if delta < 7:
        return f"{delta} дней назад"
    if delta < 30:
        weeks = delta // 7
        return f"{weeks} нед. назад"
    months = delta // 30
    return f"{months} мес. назад"


def _now_storage() -> str:
    return to_storage(utcnow())


def _calculate_pre_sla_notify_at(payload: dict) -> str:
    """Schedule pre-SLA alert based on reply SLA (default_reply_sla_minutes from last post).
    If HDE reports a tighter deadline (sla_remaining_minutes < default_reply_sla_minutes),
    that deadline wins instead.
    """
    now = utcnow()
    warning_minutes = max(config.pre_sla_warning_minutes, 0)
    total_sla_minutes = max(config.default_reply_sla_minutes, 0)

    last_post_at = parse_datetime(payload.get("last_post_date")) or now
    notify_at = last_post_at + timedelta(minutes=max(total_sla_minutes - warning_minutes, 0))

    remaining_minutes = _parse_minutes(payload.get("sla_remaining_minutes"))
    if remaining_minutes is not None and remaining_minutes < total_sla_minutes:
        hde_notify_at = now + timedelta(minutes=max(remaining_minutes - warning_minutes, 0))
        if hde_notify_at < notify_at:
            notify_at = hde_notify_at

    if notify_at < now:
        notify_at = now
    return to_storage(notify_at)


async def _send_topic_message(bot: Bot, topic_id: int, text: str) -> None:
    await bot.send_message(
        chat_id=config.group_chat_id,
        message_thread_id=topic_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _send_client_attachments(bot: Bot, topic_id: int, payload: dict) -> None:
    attachment_refs = list(payload.get("attachments") or [])
    if not attachment_refs:
        return

    photo_video_media: list[tuple[str, BufferedInputFile]] = []
    single_items: list[tuple[str, BufferedInputFile]] = []
    photo_blobs: list[tuple[bytes, str]] = []  # (content, filename) for Vision

    for ref in attachment_refs:
        try:
            attachment = await download_client_attachment(ref)
        except Exception as exc:
            logger.error("Failed to download client attachment for ticket %s: %s", _payload_value(payload, "ticket_id"), exc)
            continue

        kind = detect_telegram_media_kind(attachment)
        input_file = BufferedInputFile(attachment.content, filename=attachment.filename)
        if kind in {"photo", "video"}:
            photo_video_media.append((kind, input_file))
            if kind == "photo":
                photo_blobs.append((attachment.content, attachment.filename))
        else:
            single_items.append((kind, input_file))

    for chunk in _chunked(photo_video_media, 10):
        if len(chunk) == 1:
            kind, media = chunk[0]
            if kind == "photo":
                await bot.send_photo(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic_id,
                    photo=media,
                )
            else:
                await bot.send_video(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic_id,
                    video=media,
                )
            continue

        media_group = []
        for kind, media in chunk:
            if kind == "photo":
                media_group.append(InputMediaPhoto(media=media))
            else:
                media_group.append(InputMediaVideo(media=media))
        await bot.send_media_group(
            chat_id=config.group_chat_id,
            message_thread_id=topic_id,
            media=media_group,
        )

    for kind, media in single_items:
        if kind == "voice":
            await bot.send_voice(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                voice=media,
            )
        elif kind == "audio":
            await bot.send_audio(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                audio=media,
            )
        else:
            await bot.send_document(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                document=media,
            )

    if photo_blobs:
        await _describe_and_post_photos(bot, topic_id, photo_blobs, payload)


async def _describe_and_post_photos(
    bot: Bot,
    topic_id: int,
    photos: list[tuple[bytes, str]],
    payload: dict,
) -> None:
    """Describe photos via Vision and post a single 🔍 summary message to the topic."""
    from .vision import describe_image  # local import: keep vision lazy

    tasks = [describe_image(content, filename) for content, filename in photos]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        logger.warning("vision: gather failed for ticket %s: %s", _payload_value(payload, "ticket_id"), exc)
        return

    descriptions: list[str] = []
    for r in results:
        if isinstance(r, str) and r.strip():
            descriptions.append(r.strip())

    if not descriptions:
        return

    ticket_id = str(_payload_value(payload, "ticket_id") or "")
    if ticket_id:
        try:
            await db.append_photo_descriptions(ticket_id, descriptions)
        except Exception as exc:
            logger.warning("vision: failed to persist descriptions for ticket %s: %s", ticket_id, exc)

    if len(descriptions) == 1:
        text = f"🔍 На фото: {descriptions[0]}"
    else:
        lines = "\n".join(f"{i}. {d}" for i, d in enumerate(descriptions, 1))
        text = f"🔍 На фото:\n{lines}"

    try:
        await _send_topic_message(bot, topic_id, text)
    except Exception as exc:
        logger.warning("vision: failed to post description for ticket %s: %s", ticket_id, exc)


async def _create_topic(bot: Bot, payload: dict) -> int:
    forum_topic = await bot.create_forum_topic(
        chat_id=config.group_chat_id,
        name=_build_topic_name(payload),
        icon_color=_priority_color(_payload_value(payload, "priority")),
    )
    return forum_topic.message_thread_id


async def _rename_topic_if_needed(
    bot: Bot,
    record: db.TicketTopic,
    payload: dict,
    *,
    announce: bool = False,
) -> None:
    target_name = _build_topic_name(payload)
    current_name = make_topic_name(
        record.unique_id,
        record.company_name,
        record.ticket_name,
        record.priority or "medium",
    )

    if target_name == current_name:
        return

    try:
        await bot.edit_forum_topic(
            chat_id=config.group_chat_id,
            message_thread_id=record.topic_id,
            name=target_name,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to rename topic %d for ticket %s: %s", record.topic_id, record.ticket_id, exc)
        return

    if announce and record.ticket_name != _payload_value(payload, "ticket_name"):
        try:
            await _send_topic_message(
                bot,
                record.topic_id,
                format_ticket_renamed(record.ticket_name, _payload_value(payload, "ticket_name")),
            )
        except TelegramAPIError as exc:
            logger.error("Failed to announce rename for topic %d: %s", record.topic_id, exc)


async def _generate_summary_with_retry(
    posts,
    info,
    *,
    ticket_title: str = "",
    ticket_id: str = "",
    company_id: str = "",
    attempts: int = 3,
    pause: float = 30.0,
):
    """Call generate_ticket_summary up to *attempts* times with *pause* seconds between tries."""
    from .ai_summary import generate_ticket_summary
    for attempt in range(1, attempts + 1):
        result = await generate_ticket_summary(
            posts, info,
            ticket_title=ticket_title,
            ticket_id=ticket_id,
            company_id=company_id,
        )
        if result is not None:
            return result
        if attempt < attempts:
            logger.info(
                "AI summary attempt %d/%d failed for ticket %s, retrying in %.0fs",
                attempt, attempts, ticket_id, pause,
            )
            await asyncio.sleep(pause)
    logger.warning("AI summary failed after %d attempts for ticket %s", attempts, ticket_id)
    return None


async def retry_missing_ai_summaries(bot: Bot) -> int:
    """Find topics that never got an AI summary and retry. Returns number of summaries sent."""
    from .hde_api import HDEApiClient, HDEApiError
    from .handlers.ai_feedback import suit_feedback_kb, answer_feedback_kb, memo_feedback_kb, register_feedback_pending
    from html import escape as _html_escape

    records = await db.list_topics_missing_summary()
    sent = 0
    for record in records:
        ticket_id = record.ticket_id
        topic_id = record.topic_id
        try:
            client = HDEApiClient()
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

        from .ai_summary import _build_history_text
        all_posts = sorted(posts + comments, key=lambda p: p.date_created)
        result = await _generate_summary_with_retry(
            all_posts, info,
            ticket_title=record.ticket_name or "",
            ticket_id=ticket_id,
            company_id="",
        )
        if result is None:
            continue

        suit_line, client_line, memo_line, confidence_pct = result
        try:
            suit_label = (
                f"🧠 <b>Суть ({confidence_pct}%):</b>"
                if confidence_pct >= 40
                else "🧠 <b>Суть:</b>"
            )
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=f"{suit_label} {_html_escape(suit_line)}",
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=True,
                reply_markup=suit_feedback_kb(),
            )
            if client_line:
                await bot.send_message(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"💬 <b>Ответ клиенту:</b>\n<i>«{_html_escape(client_line)}»</i>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    disable_notification=True,
                    reply_markup=answer_feedback_kb(),
                )
            if memo_line:
                await bot.send_message(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"📋 <b>Памятка:</b>\n{_html_escape(memo_line)}",
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
                title=record.ticket_name or "",
                answer_text=client_line,
                ai_full_text=ai_full_text,
            )
            await db.update_topic(ticket_id, ai_summary_sent_at=to_storage(utcnow()))
            sent += 1
        except TelegramAPIError as exc:
            logger.warning("retry_missing_ai_summaries: failed to post summary to topic %d: %s", topic_id, exc)

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
    if not config.has_hde_api_credentials():
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

    await _post_client_history(bot, topic_id, ticket_id)

    if not all_posts:
        logger.info("No posts for ticket %s, skipping history+summary", ticket_id)
        return

    # Start AI generation immediately — runs in parallel with history posting
    from .ai_summary import generate_ticket_summary, _build_history_text
    from .handlers.ai_feedback import suit_feedback_kb, answer_feedback_kb, memo_feedback_kb, register_feedback_pending
    gen_task = asyncio.create_task(
        _generate_summary_with_retry(
            all_posts, info,
            ticket_title=ticket_title,
            ticket_id=ticket_id,
            company_id=company_id,
        )
    )

    # Post history while generation runs in background
    messages = format_ticket_history(all_posts, info)
    for text in messages:
        try:
            await bot.send_message(
                chat_id=config.group_chat_id,
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
    from html import escape as _html_escape
    result = await gen_task
    if result is None:
        logger.info("AI summary not generated for ticket %s", ticket_id)
        return

    suit_line, client_line, memo_line, confidence_pct = result
    try:
        # Message 1 — Суть
        suit_label = (
            f"🧠 <b>Суть ({confidence_pct}%):</b>"
            if confidence_pct >= 40
            else "🧠 <b>Суть:</b>"
        )
        suit_text = f"{suit_label} {_html_escape(suit_line)}"
        await bot.send_message(
            chat_id=config.group_chat_id,
            message_thread_id=topic_id,
            text=suit_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=True,
            reply_markup=suit_feedback_kb(),
        )
        # Message 2 — Ответ клиенту
        if client_line:
            await bot.send_message(
                chat_id=config.group_chat_id,
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
        # Message 3 — Памятка для специалиста
        if memo_line:
            await bot.send_message(
                chat_id=config.group_chat_id,
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
        )
        await db.update_topic(ticket_id, ai_summary_sent_at=to_storage(utcnow()))
    except TelegramAPIError as exc:
        logger.warning("Failed to post AI summary to topic %d: %s", topic_id, exc)

    try:
        from .ticket_fields import apply_ticket_fields
        autofill_history = _build_history_text(all_posts, info)
        await apply_ticket_fields(bot, ticket_id, topic_id, autofill_history)
    except Exception as exc:
        logger.warning("Ticket field auto-fill failed for %s: %s", ticket_id, exc)


async def _ensure_active_topic(
    bot: Bot,
    payload: dict,
    *,
    announce_assignment: bool = False,
) -> db.TicketTopic:
    ticket_id = _payload_value(payload, "ticket_id")
    record = await db.get_topic(ticket_id)
    metadata = _topic_metadata(payload)
    should_announce_assignment = False
    reassignment = False

    if record is None or record.is_deleted:
        topic_id = await _create_topic(bot, payload)
        await db.upsert_topic(ticket_id, topic_id, topic_state="active", **metadata)
        await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
        # Send assignment notification FIRST (before summary and history)
        if announce_assignment:
            notif_text = format_assignment_message(
                display_id=ticket_id,
                company_name=metadata["company_name"],
                ticket_name=metadata["ticket_name"],
                status=metadata["status"],
                priority=metadata["priority"],
                link=metadata["hde_link"],
                is_reassignment=False,
            )
            try:
                await _send_topic_message(bot, topic_id, notif_text)
            except TelegramAPIError as exc:
                logger.error("Failed to send assignment message to topic %d: %s", topic_id, exc)
        await _post_ticket_history(
            bot, ticket_id, topic_id,
            ticket_title=_payload_value(payload, "ticket_name"),
            company_id=_payload_value(payload, "company_id"),
        )
        should_announce_assignment = False  # already sent above
    elif record.is_pending_delete:
        try:
            await bot.reopen_forum_topic(
                chat_id=config.group_chat_id,
                message_thread_id=record.topic_id,
            )
        except TelegramAPIError as exc:
            if any(k in str(exc).lower() for k in ("thread not found", "not found", "deleted")):
                logger.warning(
                    "Topic %d for ticket %s not found in Telegram (pending_delete), recreating",
                    record.topic_id, ticket_id,
                )
                await db.mark_topic_deleted(ticket_id)
                return await _ensure_active_topic(bot, payload, announce_assignment=announce_assignment)
            logger.error("Failed to reopen topic %d for ticket %s: %s", record.topic_id, ticket_id, exc)
        await _rename_topic_if_needed(bot, record, payload)
        await db.upsert_topic(ticket_id, record.topic_id, topic_state="active", delete_after_at=None, deleted_at=None, **metadata)
        await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
        should_announce_assignment = announce_assignment
        reassignment = True
    else:
        # On reassignment: verify the topic still exists in Telegram.
        # Users can manually delete topics — Telegram doesn't notify the bot,
        # so DB stays "active". We probe via edit_forum_topic which throws
        # "thread not found" if the topic no longer exists.
        if announce_assignment:
            try:
                await bot.edit_forum_topic(
                    chat_id=config.group_chat_id,
                    message_thread_id=record.topic_id,
                    name=_build_topic_name(payload),
                )
            except TelegramAPIError as exc:
                err_lower = str(exc).lower()
                if "topic_not_modified" in err_lower:
                    # Same name — Telegram rejects the edit but topic is alive. Not an error.
                    pass
                elif any(k in err_lower for k in ("topic_id_invalid", "thread not found", "not found", "deleted")):
                    logger.warning(
                        "Topic %d for ticket %s no longer exists in Telegram, recreating",
                        record.topic_id, ticket_id,
                    )
                    await db.mark_topic_deleted(ticket_id)
                    return await _ensure_active_topic(bot, payload, announce_assignment=announce_assignment)
                else:
                    logger.error("Failed to verify topic %d: %s", record.topic_id, exc)
        else:
            await _rename_topic_if_needed(bot, record, payload)
        await db.update_topic(
            ticket_id,
            unique_id=metadata["unique_id"],
            company_name=metadata["company_name"],
            ticket_name=metadata["ticket_name"],
            priority=metadata["priority"],
            status=metadata["status"],
            owner_id=metadata["owner_id"],
            owner_name=metadata["owner_name"],
            hde_link=metadata["hde_link"],
            topic_state="active",
            delete_after_at=None,
            deleted_at=None,
        )
        await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
        # No should_announce_assignment here — avoids duplicates when HDE
        # fires the same owner_changed webhook twice

    record = await db.get_topic(ticket_id)
    if record is None:
        raise RuntimeError(f"Topic record for ticket {ticket_id} was not created")

    if should_announce_assignment:
        text = format_assignment_message(
            display_id=record.ticket_id,
            company_name=record.company_name,
            ticket_name=record.ticket_name,
            status=record.status,
            priority=record.priority,
            link=record.hde_link,
            is_reassignment=reassignment,
        )
        try:
            await _send_topic_message(bot, record.topic_id, text)
        except TelegramAPIError as exc:
            err = str(exc).lower()
            if "thread not found" in err or "topic_deleted" in err or "not found" in err:
                # Topic was manually deleted in Telegram — recreate it
                logger.warning(
                    "Topic %d for ticket %s not found in Telegram, recreating",
                    record.topic_id, ticket_id,
                )
                await db.mark_topic_deleted(ticket_id)
                new_topic_id = await _create_topic(bot, payload)
                await db.upsert_topic(ticket_id, new_topic_id, topic_state="active", **metadata)
                await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
                try:
                    await _send_topic_message(bot, new_topic_id, text)
                except TelegramAPIError as exc2:
                    logger.error("Failed to send to recreated topic %d: %s", new_topic_id, exc2)
                await _post_ticket_history(
                    bot, ticket_id, new_topic_id,
                    ticket_title=record.ticket_name,
                )
                record = await db.get_topic(ticket_id)
            else:
                logger.error("Failed to send assignment message to topic %d: %s", record.topic_id, exc)

    return record


async def _schedule_pre_sla(ticket_id: str, payload: dict, last_client_reply_at: str) -> None:
    notify_at = _calculate_pre_sla_notify_at(payload)
    logger.info(
        "PRESLA-DIAG schedule ticket=%s notify_at=%s lcr=%s",
        ticket_id, notify_at, last_client_reply_at,
    )
    await db.update_topic(
        ticket_id,
        last_client_reply_at=last_client_reply_at,
        pre_sla_notify_at=notify_at,
        pre_sla_sent_at=None,
    )


async def _delete_topic_now(bot: Bot, record: db.TicketTopic) -> bool:
    try:
        await bot.delete_forum_topic(
            chat_id=config.group_chat_id,
            message_thread_id=record.topic_id,
        )
    except TelegramAPIError as exc:
        err = str(exc).lower()
        if any(k in err for k in ("topic_id_invalid", "thread not found", "topic_deleted", "not found")):
            await db.mark_topic_deleted(record.ticket_id)
            logger.info(
                "Topic %d for ticket %s already gone in Telegram, marked deleted in DB",
                record.topic_id, record.ticket_id,
            )
            return True
        logger.error("Failed to delete topic %d for ticket %s: %s", record.topic_id, record.ticket_id, exc)
        return False

    await db.mark_topic_deleted(record.ticket_id)
    logger.info("Deleted topic %d for ticket %s", record.topic_id, record.ticket_id)
    return True


async def handle_owner_changed(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    async with _ticket_lock(ticket_id):
        await _handle_owner_changed_locked(bot, payload, ticket_id)


def _is_work_time() -> bool:
    from .work_schedule import is_work_time
    return is_work_time()


async def _handle_owner_changed_locked(bot: Bot, payload: dict, ticket_id: str) -> None:
    record = await db.get_topic(ticket_id)

    # Always guard deleted topics first — delayed webhooks must not recreate them
    if record is not None and record.is_deleted:
        logger.info("Ignoring owner_changed for already-deleted ticket %s", ticket_id)
        return

    if _effective_owner_match(payload):
        # Announce assignment only during work hours
        await _ensure_active_topic(bot, payload, announce_assignment=_is_work_time())
        return

    if record is None:
        logger.info("Ignoring owner_changed for untracked ticket %s", ticket_id)
        return

    if record.is_pending_delete:
        await _try_delete_pre_sla_message(bot, record)
        await db.update_topic(
            ticket_id,
            unique_id=_display_id(payload),
            company_name=_payload_value(payload, "company_name"),
            ticket_name=_payload_value(payload, "ticket_name"),
            priority=_payload_value(payload, "priority"),
            status=_payload_value(payload, "status"),
            owner_id=_payload_value(payload, "owner_id"),
            owner_name=_payload_value(payload, "owner_name"),
            hde_link=_payload_value(payload, "link"),
            pre_sla_notify_at=None,
            pre_sla_sent_at=None,
            pre_sla_message_id=None,
        )
        logger.info("Ticket %s already pending delete; skipping duplicate close", ticket_id)
        return

    delete_after = to_storage(utcnow() + timedelta(minutes=10))
    if _is_work_time():
        try:
            await _send_topic_message(
                bot,
                record.topic_id,
                format_unassigned_message(record.unique_id, _payload_value(payload, "link")),
            )
        except TelegramAPIError as exc:
            logger.error("Failed to send unassigned message to topic %d: %s", record.topic_id, exc)

    try:
        await bot.close_forum_topic(
            chat_id=config.group_chat_id,
            message_thread_id=record.topic_id,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to close topic %d for ticket %s: %s", record.topic_id, record.ticket_id, exc)

    await db.delete_reply_draft(record.topic_id)
    await _try_delete_pre_sla_message(bot, record)
    await db.update_topic(
        ticket_id,
        unique_id=_display_id(payload),
        company_name=_payload_value(payload, "company_name"),
        ticket_name=_payload_value(payload, "ticket_name"),
        priority=_payload_value(payload, "priority"),
        status=_payload_value(payload, "status"),
        owner_id=_payload_value(payload, "owner_id"),
        owner_name=_payload_value(payload, "owner_name"),
        hde_link=_payload_value(payload, "link"),
        topic_state="pending_delete",
        delete_after_at=delete_after,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
    )


async def handle_assigned_on_create(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    if not _effective_owner_match(payload):
        logger.info("Ignoring assigned_on_create for ticket %s because owner does not match target user", ticket_id)
        return

    async with _ticket_lock(ticket_id):
        await _ensure_active_topic(bot, payload, announce_assignment=_is_work_time())


async def handle_ticket_updated(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    record = await db.get_topic(ticket_id)
    if record is None or record.is_deleted:
        logger.info("Ignoring ticket_updated for unknown ticket %s", ticket_id)
        return

    await _rename_topic_if_needed(bot, record, payload, announce=True)
    await db.update_topic(
        ticket_id,
        unique_id=_display_id(payload),
        company_name=_payload_value(payload, "company_name"),
        ticket_name=_payload_value(payload, "ticket_name"),
        priority=_payload_value(payload, "priority"),
        status=_payload_value(payload, "status"),
        owner_id=_payload_value(payload, "owner_id"),
        owner_name=_payload_value(payload, "owner_name"),
        hde_link=_payload_value(payload, "link"),
    )


async def handle_client_reply(bot: Bot, payload: dict) -> None:
    if not _is_work_time():
        return

    ticket_id = _payload_value(payload, "ticket_id")
    _existing = await db.get_topic(ticket_id)
    if _existing is not None and _existing.is_deleted:
        logger.info("Ignoring client_reply for deleted ticket %s", ticket_id)
        return

    record = await _ensure_active_topic(bot, payload)
    reply_text = format_client_reply(
        user_name=_payload_value(payload, "user_name"),
        message=_payload_value(payload, "message"),
        sla_remaining=payload.get("sla_remaining_minutes"),
        link=record.hde_link or _payload_value(payload, "link"),
        date_str=_payload_value(payload, "last_post_date"),
    )
    try:
        await _send_topic_message(bot, record.topic_id, reply_text)
    except TelegramAPIError as exc:
        err_lower = str(exc).lower()
        if any(k in err_lower for k in ("thread not found", "topic_id_invalid", "topic_deleted", "not found")):
            logger.warning(
                "Topic %d for ticket %s missing in Telegram on client_reply, recreating",
                record.topic_id, ticket_id,
            )
            await db.mark_topic_deleted(ticket_id)
            record = await _ensure_active_topic(bot, payload, announce_assignment=True)
            try:
                await _send_topic_message(bot, record.topic_id, reply_text)
            except TelegramAPIError as exc2:
                logger.error("Failed to resend client reply to recreated topic %d: %s", record.topic_id, exc2)
        else:
            logger.error("Failed to send client reply to topic %d: %s", record.topic_id, exc)

    try:
        await _send_client_attachments(bot, record.topic_id, payload)
    except TelegramAPIError as exc:
        logger.error("Failed to send client attachments to topic %d: %s", record.topic_id, exc)

    reply_at = parse_datetime(payload.get("last_post_date")) or utcnow()
    # Only start the SLA timer on the FIRST unanswered client message.
    # Subsequent client messages don't reset the deadline — only a staff
    # reply resets it (handle_staff_reply clears pre_sla_notify_at).
    timer_active = record.pre_sla_notify_at is not None and record.pre_sla_sent_at is None
    logger.info(
        "PRESLA-DIAG client_reply ticket=%s notify_at=%r sent_at=%r timer_active=%s "
        "branch=%s reply_at=%s",
        record.ticket_id, record.pre_sla_notify_at, record.pre_sla_sent_at,
        timer_active, "else-update-lcr" if timer_active else "schedule",
        to_storage(reply_at),
    )
    if not timer_active:
        await _schedule_pre_sla(record.ticket_id, payload, to_storage(reply_at))
    else:
        # Still update last_client_reply_at so we track the latest message time
        await db.update_topic(record.ticket_id, last_client_reply_at=to_storage(reply_at))


async def _maybe_update_pattern(title: str, staff_text: str, ticket_id: str) -> None:
    """Strengthen existing pattern or create new one from high-quality operator reply."""
    from .ai_summary import _detect_equipment
    from . import db as _db3
    import difflib

    equipment = _detect_equipment(title, staff_text)

    # Check if similar pattern exists → increment use_count
    patterns = await _db3.list_solution_patterns(limit=100)
    for p in patterns:
        if p["equipment"] != equipment:
            continue
        ratio = difflib.SequenceMatcher(
            None, p["problem_type"].lower(), title.lower()
        ).ratio()
        if ratio >= 0.7:
            await _db3.increment_pattern_use(p["id"])
            logger.debug(
                "Pattern %d reinforced for ticket %s (ratio=%.2f)",
                p["id"], ticket_id, ratio,
            )
            return

    # No existing pattern — extract new one via Gemini (non-fatal)
    import aiohttp as _aiohttp
    from .config import config as _cfg
    if not _cfg.gemini_api_key:
        return

    prompt = (
        "Из ответа технического специалиста извлеки:\n"
        "- problem_type: тип проблемы (5-10 слов)\n"
        "- steps: шаги решения через →\n"
        "Верни JSON: {\"problem_type\": ..., \"steps\": ...}\n"
        "Только JSON. Если шагов нет — верни {}.\n\n"
        f"Тема: {title}\nОтвет специалиста: {staff_text[:600]}"
    )
    from .llm_semaphore import LLM_SEMAPHORE  # noqa: PLC0415
    try:
        async with LLM_SEMAPHORE, _aiohttp.ClientSession() as session:
            async with session.post(
                (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "gemini-2.5-flash:generateContent"
                ),
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300},
                },
                params={"key": _cfg.gemini_api_key},
                timeout=_aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        import re as _re, json as _json
        raw = _re.sub(r"^```[^\n]*\n?", "", raw).rstrip("`").strip()
        parsed = _json.loads(raw)
        pt = (parsed.get("problem_type") or "").strip()
        st = (parsed.get("steps") or "").strip()
        if pt and st and not await _db3.pattern_exists_similar(equipment, pt):
            await _db3.save_solution_pattern(
                problem_type=pt, steps=st, source="implicit", equipment=equipment
            )
            logger.info(
                "New implicit pattern created for ticket %s: %r", ticket_id, pt
            )
    except Exception as exc:
        logger.warning("_maybe_update_pattern Gemini call failed: %s", exc)


async def _implicit_feedback(record: "db.TicketTopic", staff_text: str) -> None:
    """Auto-learn from diff between AI suggestion and operator's actual reply."""
    import difflib
    import re as _re
    from html import unescape

    pending = await db.get_ai_feedback_pending(record.topic_id)
    if not pending:
        return

    ai_text = (pending.get("answer_text") or "").strip()
    # Strip HTML from staff reply
    clean_staff = unescape(_re.sub(r"<[^>]+>", "", staff_text)).strip()
    if not ai_text or not clean_staff:
        return

    ratio = difflib.SequenceMatcher(None, ai_text.lower(), clean_staff.lower()).ratio()
    logger.debug(
        "Implicit feedback for topic %d: ratio=%.2f ai=%r staff=%r",
        record.topic_id, ratio, ai_text[:60], clean_staff[:60],
    )

    if ratio >= 0.7:
        # Operator sent nearly the same text — AI suggestion was good
        await db.delete_ai_feedback_pending(record.topic_id)
        content = f"Тема: {pending['title']}\n\n{pending['history']}"
        # Удалить старый implicit_good для этого тикета (один тикет = одна запись)
        try:
            await db.delete_knowledge_item_by_ticket(
                pending["ticket_id"], "implicit_good"
            )
        except Exception as exc:
            logger.warning("delete_knowledge_item_by_ticket failed: %s", exc)
        from .knowledge.indexer import index_knowledge_item
        await index_knowledge_item(
            source="implicit_good",
            content=content,
            ticket_id=pending["ticket_id"],
            title=pending["title"],
            quality="good",
        )
        logger.info(
            "Implicit 👍 for ticket %s (ratio=%.2f)", pending["ticket_id"], ratio
        )
        # Save sample for prompt optimizer
        try:
            await db.save_optimization_sample(
                ticket_id=pending["ticket_id"],
                title=pending.get("title", ""),
                history=pending.get("history", ""),
                ai_answer=pending.get("answer_text", ""),
                op_answer=clean_staff,
                outcome="accepted",
            )
        except Exception as exc:
            logger.warning("save_optimization_sample failed: %s", exc)
        # Reinforce or create solution pattern (only for high-confidence matches)
        if ratio >= 0.85:
            try:
                await _maybe_update_pattern(pending["title"], clean_staff, pending["ticket_id"])
            except Exception as exc:
                logger.warning("Pattern update failed: %s", exc)
    elif ratio <= 0.35:
        # Operator wrote something significantly different — save as correction
        await db.delete_ai_feedback_pending(record.topic_id)
        content = (
            f"Тема: {pending['title']}\n\n"
            f"{pending['history']}\n\n"
            f"Правильный ответ: {clean_staff}"
        )
        from .knowledge.indexer import index_knowledge_item
        await index_knowledge_item(
            source="implicit_corrected",
            content=content,
            ticket_id=pending["ticket_id"],
            title=pending["title"],
            quality="corrected",
        )
        logger.info(
            "Implicit ✏️ for ticket %s (ratio=%.2f)", pending["ticket_id"], ratio
        )
        # Save sample for prompt optimizer
        try:
            await db.save_optimization_sample(
                ticket_id=pending["ticket_id"],
                title=pending.get("title", ""),
                history=pending.get("history", ""),
                ai_answer=pending.get("answer_text", ""),
                op_answer=clean_staff,
                outcome="corrected",
            )
        except Exception as exc:
            logger.warning("save_optimization_sample failed: %s", exc)
        # Update wiki article with operator's actual answer (non-fatal)
        try:
            from .wiki.builder import build_or_update_wiki_article
            await build_or_update_wiki_article(
                title=pending["title"],
                content=content,
                ticket_id=pending["ticket_id"],
            )
        except Exception as exc:
            logger.warning("Wiki update failed after implicit correction: %s", exc)
    # 0.35–0.7: ambiguous edit, skip to avoid noise


async def handle_staff_reply(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    record = await db.get_topic(ticket_id)
    if record is None or record.is_deleted:
        logger.info("Ignoring staff_reply for unknown ticket %s", ticket_id)
        return

    reply_at = parse_datetime(payload.get("last_post_date")) or utcnow()
    logger.info(
        "PRESLA-DIAG staff_reply ticket=%s clearing_pre_sla had_notify_at=%r "
        "had_sent_at=%r staff_reply_at=%s last_client_reply_at=%r",
        ticket_id, record.pre_sla_notify_at, record.pre_sla_sent_at,
        to_storage(reply_at), record.last_client_reply_at,
    )
    await _try_delete_pre_sla_message(bot, record)
    await db.update_topic(
        ticket_id,
        unique_id=_display_id(payload),
        company_name=_payload_value(payload, "company_name"),
        ticket_name=_payload_value(payload, "ticket_name"),
        priority=_payload_value(payload, "priority"),
        status=_payload_value(payload, "status"),
        owner_id=_payload_value(payload, "owner_id"),
        owner_name=_payload_value(payload, "owner_name"),
        hde_link=_payload_value(payload, "link"),
        last_staff_reply_at=to_storage(reply_at),
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
    )

    # Implicit feedback: compare AI suggestion with what operator actually sent
    staff_text = _payload_value(payload, "message") or ""
    if staff_text and record:
        try:
            await _implicit_feedback(record, staff_text)
        except Exception as exc:
            logger.warning("Implicit feedback failed for ticket %s: %s", ticket_id, exc)


async def handle_ticket_closed(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    record = await db.get_topic(ticket_id)
    if record is None or record.is_deleted:
        logger.info("Ignoring ticket_closed for unknown ticket %s", ticket_id)
        return
    if record.is_pending_delete:
        logger.info("Ticket %s already pending delete, skipping duplicate ticket_closed", ticket_id)
        return

    await _try_delete_pre_sla_message(bot, record)
    await db.update_topic(
        ticket_id,
        unique_id=_display_id(payload),
        company_name=_payload_value(payload, "company_name"),
        ticket_name=_payload_value(payload, "ticket_name"),
        priority=_payload_value(payload, "priority"),
        status=_payload_value(payload, "status"),
        owner_id=_payload_value(payload, "owner_id"),
        owner_name=_payload_value(payload, "owner_name"),
        hde_link=_payload_value(payload, "link"),
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
    )

    ok = await _delete_topic_now(bot, record)
    if ok:
        logger.info("Ticket %s completed, topic %d deleted immediately", ticket_id, record.topic_id)
    else:
        logger.warning(
            "Ticket %s completed but topic %d could not be deleted (will retry via reconcile)",
            ticket_id, record.topic_id,
        )


def _pre_sla_minutes_left(record: "db.TicketTopic") -> int:
    """Вычисляет целые минуты до SLA (floor, как в HDE). 0 = меньше 1 минуты."""
    import math
    deadline = parse_datetime(record.pre_sla_notify_at)
    if deadline is None:
        return config.pre_sla_warning_minutes
    sla_deadline = deadline + timedelta(minutes=config.pre_sla_warning_minutes)
    remaining = (sla_deadline - utcnow()).total_seconds() / 60
    return max(0, math.floor(remaining))


def _pre_sla_destination(record: "db.TicketTopic") -> tuple[int, int | None]:
    """Возвращает (chat_id, thread_id) для pre-SLA сообщения.

    Если тикет назначен — топик тикета.
    Если нет исполнителя — General (general_topic_id).
    Fallback: если General не настроен, шлём в топик тикета.
    """
    has_owner = bool(record.owner_id.strip())
    if has_owner:
        return config.group_chat_id, record.topic_id
    if config.general_topic_id is not None:
        return config.group_chat_id, config.general_topic_id
    return config.group_chat_id, record.topic_id


def _pre_sla_text(record: "db.TicketTopic", minutes_left: int) -> str:
    has_owner = bool(record.owner_id.strip())
    if has_owner:
        return format_pre_sla_alert_topic(
            minutes_left=minutes_left,
            ticket_name=record.ticket_name,
            link=record.hde_link,
        )
    return format_pre_sla_alert_general(
        minutes_left=minutes_left,
        ticket_name=record.ticket_name,
        company_name=record.company_name,
        link=record.hde_link,
    )


async def _hde_staff_replied_since(ticket_id: str, since_storage: Optional[str]) -> bool:
    """Return True if operator posted in HDE after last client reply.

    Fails open (returns False) on any error so pre-SLA is never silently swallowed.
    Skips check when presla_hde_verify=False or hde_owner_id not configured.
    """
    if not config.presla_hde_verify:
        return False
    owner_id_str = config.hde_owner_id.strip()
    if not owner_id_str:
        return False
    try:
        owner_id = int(owner_id_str)
    except ValueError:
        return False

    since_dt = parse_datetime(since_storage)

    try:
        from .hde_api import HDEApiClient
        client = HDEApiClient()
        posts = await client.get_ticket_posts(ticket_id, limit=5)
        # get_ticket_posts returns oldest-first; iterate newest-first
        for post in reversed(posts):
            if post.user_id != owner_id:
                continue
            if since_dt is None:
                return True
            try:
                from datetime import timezone, timedelta
                _msk = timezone(timedelta(hours=3))
                post_dt = datetime.strptime(
                    post.date_created, "%H:%M:%S %d.%m.%Y"
                ).replace(tzinfo=_msk).astimezone(timezone.utc)
            except (ValueError, AttributeError):
                continue
            if post_dt > since_dt:
                return True
        return False
    except Exception as exc:
        logger.warning("pre-SLA HDE verify failed for ticket %s: %s", ticket_id, exc)
        return False


async def send_reassurance_to_client(bot: Bot, record: db.TicketTopic) -> None:
    """Send reassurance post to client via HDE and notify topic. Idempotent via reassurance_sent_at."""
    from .hde_api import HDEApiClient, HDEApiError
    try:
        client = HDEApiClient()
        await client.add_post(record.ticket_id, config.reassurance_text)
    except HDEApiError as exc:
        logger.warning("Reassurance post failed for ticket %s: %s", record.ticket_id, exc)
        return  # don't mark sent — retry next tick

    await db.update_topic(record.ticket_id, reassurance_sent_at=to_storage(utcnow()))

    try:
        await bot.send_message(
            chat_id=config.group_chat_id,
            message_thread_id=record.topic_id,
            text=(
                "🤖 <b>Автоответ клиенту отправлен</b>\n"
                f"<i>{config.reassurance_text}</i>"
            ),
            parse_mode="HTML",
            disable_notification=True,
        )
    except TelegramAPIError as exc:
        logger.warning("Failed to notify topic about reassurance for %s: %s", record.ticket_id, exc)


async def send_pre_sla_alert(bot: Bot, record: db.TicketTopic) -> None:
    minutes_left = _pre_sla_minutes_left(record)
    chat_id, thread_id = _pre_sla_destination(record)
    text = _pre_sla_text(record, minutes_left)

    msg = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await db.mark_pre_sla_sent(record.ticket_id, message_id=msg.message_id)


async def update_pre_sla_alert(bot: Bot, record: db.TicketTopic) -> None:
    """Удаляет старое pre-SLA сообщение и присылает новое с актуальным счётчиком."""
    chat_id, thread_id = _pre_sla_destination(record)

    if record.pre_sla_message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=record.pre_sla_message_id)
        except TelegramAPIError:
            pass

    minutes_left = _pre_sla_minutes_left(record)
    text = _pre_sla_text(record, minutes_left)

    msg = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await db.mark_pre_sla_sent(record.ticket_id, message_id=msg.message_id)


async def _try_delete_pre_sla_message(bot: Bot, record: db.TicketTopic) -> None:
    """Удаляет pre-SLA сообщение из Telegram если оно было отправлено."""
    if not record.pre_sla_message_id:
        return
    chat_id, _ = _pre_sla_destination(record)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=record.pre_sla_message_id)
    except TelegramAPIError:
        pass


async def delete_pending_topic(bot: Bot, record: db.TicketTopic) -> bool:
    return await _delete_topic_now(bot, record)


async def sync_ticket_topic(bot: Bot, payload: dict) -> db.TicketTopic:
    """Upsert a topic for one ticket without sending an assignment announcement.

    Used by /refresh to bring Telegram topics in sync with HDE state.
    Serialised per-ticket via _ticket_lock to avoid race conditions.
    """
    ticket_id = _payload_value(payload, "ticket_id")
    async with _ticket_lock(ticket_id):
        return await _ensure_active_topic(bot, payload, announce_assignment=False)


async def _post_client_history(bot: Bot, topic_id: int, ticket_id: str) -> None:
    """Fetch client's past tickets from HDE and post a summary to the topic.

    Silently skips on any error or if no past tickets exist.
    """
    try:
        from .hde_api import HDEApiClient
        client = HDEApiClient()
        info = await client.get_ticket_info(ticket_id)
        if not info or not info.client_id:
            return
        tickets = await client.get_client_tickets(info.client_id, limit=10)
        past = [t for t in tickets if str(t.get("id", "")) != str(ticket_id)]
        if not past:
            return
        recent_titles = [t["subject"] for t in past[:5] if t.get("subject")]
        last_date = _relative_date(past[0].get("date_created"))
        text = format_client_history(
            client_name=info.client_name,
            total=len(past),
            recent_titles=recent_titles,
            last_ticket_date=last_date,
        )
        if text:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=True,
            )
    except Exception as exc:
        logger.warning("Client history failed for ticket %s: %s", ticket_id, exc)
