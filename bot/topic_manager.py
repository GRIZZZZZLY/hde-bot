from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
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
    format_client_reply,
    format_pre_sla_alert,
    format_ticket_closed,
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


async def _post_ticket_history(
    bot: Bot,
    ticket_id: str,
    topic_id: int,
    ticket_title: str = "",
) -> None:
    """Fetch conversation history from HDE and post it to the topic (oldest→newest).

    After history messages, generates and posts an AI summary (if GEMINI_API_KEY is set).
    """
    if not config.has_hde_api_credentials():
        return
    from .hde_api import HDEApiClient, HDEApiError
    try:
        client = HDEApiClient()
        info = await client.get_ticket_info(ticket_id)
        posts = await client.get_ticket_posts(ticket_id)
    except HDEApiError as exc:
        logger.warning("Could not fetch history for ticket %s: %s", ticket_id, exc)
        return
    except Exception as exc:
        logger.error("Unexpected error fetching history for ticket %s: %s", ticket_id, exc)
        return

    if not posts:
        return

    # Post history first
    messages = format_ticket_history(posts, info)
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

    # Post AI summary LAST
    from .ai_summary import generate_ticket_summary, _build_history_text
    from .handlers.ai_feedback import make_ai_feedback_keyboard, register_feedback_pending
    summary = await generate_ticket_summary(
        posts, info, ticket_title=ticket_title, ticket_id=ticket_id
    )
    if summary:
        try:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=summary,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=make_ai_feedback_keyboard(),
            )
            plain_history = _build_history_text(posts, info)
            await register_feedback_pending(
                topic_id=topic_id,
                ticket_id=ticket_id,
                history=plain_history,
                title=ticket_title,
            )
        except TelegramAPIError as exc:
            logger.warning("Failed to post AI summary to topic %d: %s", topic_id, exc)


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
                if any(k in str(exc).lower() for k in ("thread not found", "not found", "deleted")):
                    logger.warning(
                        "Topic %d for ticket %s no longer exists in Telegram, recreating",
                        record.topic_id, ticket_id,
                    )
                    await db.mark_topic_deleted(ticket_id)
                    return await _ensure_active_topic(bot, payload, announce_assignment=announce_assignment)
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

    if _effective_owner_match(payload):
        # Announce assignment only during work hours
        await _ensure_active_topic(bot, payload, announce_assignment=_is_work_time())
        return

    if record is None or record.is_deleted:
        logger.info("Ignoring owner_changed for untracked ticket %s", ticket_id)
        return

    if record.is_pending_delete:
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

    record = await _ensure_active_topic(bot, payload)
    try:
        await _send_topic_message(
            bot,
            record.topic_id,
            format_client_reply(
                user_name=_payload_value(payload, "user_name"),
                message=_payload_value(payload, "message"),
                sla_remaining=payload.get("sla_remaining_minutes"),
                link=record.hde_link or _payload_value(payload, "link"),
            ),
        )
    except TelegramAPIError as exc:
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
    if not timer_active:
        await _schedule_pre_sla(record.ticket_id, payload, to_storage(reply_at))
    else:
        # Still update last_client_reply_at so we track the latest message time
        await db.update_topic(record.ticket_id, last_client_reply_at=to_storage(reply_at))


async def handle_staff_reply(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    record = await db.get_topic(ticket_id)
    if record is None or record.is_deleted:
        logger.info("Ignoring staff_reply for unknown ticket %s", ticket_id)
        return

    reply_at = parse_datetime(payload.get("last_post_date")) or utcnow()
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
    )


async def handle_ticket_closed(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    record = await db.get_topic(ticket_id)
    if record is None or record.is_deleted:
        logger.info("Ignoring ticket_closed for unknown ticket %s", ticket_id)
        return
    if record.is_pending_delete:
        logger.info("Ticket %s already pending delete, skipping duplicate ticket_closed", ticket_id)
        return

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
    )

    if _is_work_time():
        try:
            await _send_topic_message(bot, record.topic_id, format_ticket_closed())
        except TelegramAPIError as exc:
            logger.error("Failed to send closed message to topic %d: %s", record.topic_id, exc)

    await _delete_topic_now(bot, record)
    logger.info("Ticket %s completed, topic %d deleted immediately", ticket_id, record.topic_id)


async def send_pre_sla_alert(bot: Bot, record: db.TicketTopic) -> None:
    await bot.send_message(
        chat_id=config.personal_chat_id,
        text=format_pre_sla_alert(
            display_id=record.unique_id,
            ticket_name=record.ticket_name,
            company_name=record.company_name,
            minutes_left=config.pre_sla_warning_minutes,
            link=record.hde_link,
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await db.mark_pre_sla_sent(record.ticket_id)


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
