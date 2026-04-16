"""
Notifications for unassigned tickets in the General Telegram topic.

Enabled only when GENERAL_TOPIC_ID is set in config.
"""
from __future__ import annotations

import logging
import zoneinfo
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .config import config

_MSK = zoneinfo.ZoneInfo("Europe/Moscow")
# Tracks unique ticket IDs saved to pending since last flush (in-memory, resets on restart)
_overnight_pending_ids: set[str] = set()

def _is_our_operator(payload: dict) -> bool:
    """Return True if the current owner in this payload is our operator."""
    return config.matches_owner(
        _payload_str(payload, "owner_id"),
        _payload_str(payload, "owner_name"),
    )

logger = logging.getLogger(__name__)


_UNASSIGNED_MARKERS = frozenset({
    "неприсвоенный", "неназначенный", "неназначенно", "unassigned",
})


def _is_unassigned(owner_name: str, department: str, target_dept: str) -> bool:
    """Return True if ticket qualifies for General notification."""
    name = owner_name.strip().lower()
    has_owner = bool(name) and not any(m in name for m in _UNASSIGNED_MARKERS)
    if has_owner:
        return False
    if target_dept and department and department.strip().lower() != target_dept.strip().lower():
        return False
    return True


def _format_overnight_summary(total: int, assigned: int, unassigned: int) -> str:
    now_msk = datetime.now(timezone.utc).astimezone(_MSK)
    from_dt = (now_msk - timedelta(days=1)).replace(
        hour=config.work_hour_end, minute=0, second=0, microsecond=0,
    )
    dept = config.unassigned_department or "отдел"
    return "\n".join([
        "📊 <b>Сводка за ночь</b>",
        f"🕕 {from_dt.strftime('%H:%M %d.%m')} — {now_msk.strftime('%H:%M %d.%m')}",
        "",
        f"📬 Поступило за ночь: {total}",
        f"✅ Назначены до открытия: {assigned}",
        f"⚠️ Неприсвоенных ({dept}): {unassigned}",
    ])


def _format_general_message(display_id: str, ticket_name: str, link: str) -> str:
    parts = [
        "🆕 <b>Неприсвоенный тикет</b>\n",
        f"#{display_id} — {ticket_name}",
    ]
    if link:
        parts.append(f'\n<a href="{link}">Открыть в HDE</a>')
    return "\n".join(parts)


def _take_keyboard(ticket_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤙 Забрать", callback_data=f"take:{ticket_id}"),
    ]])


async def _send(bot: Bot, text: str, ticket_id: str) -> int | None:
    """Send a message to the General topic. Returns message_id or None on failure."""
    assert config.general_topic_id is not None
    try:
        # thread_id=1 is the General topic in forum groups — Telegram may reject it
        # when the group was created without explicit topics; omit it so the message
        # falls through to the main (General) thread automatically.
        kwargs: dict = dict(
            chat_id=config.group_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_take_keyboard(ticket_id),
        )
        if config.general_topic_id != 1:
            kwargs["message_thread_id"] = config.general_topic_id
        msg = await bot.send_message(**kwargs)
        return msg.message_id
    except TelegramAPIError as exc:
        logger.error("Failed to send General notification: %s", exc)
        return None


async def _edit(bot: Bot, message_id: int, text: str, ticket_id: str) -> None:
    assert config.general_topic_id is not None
    try:
        await bot.edit_message_text(
            chat_id=config.group_chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_take_keyboard(ticket_id),
        )
    except TelegramAPIError as exc:
        logger.error("Failed to edit General notification %d: %s", message_id, exc)


async def _delete(bot: Bot, message_id: int) -> None:
    try:
        await bot.delete_message(
            chat_id=config.group_chat_id,
            message_id=message_id,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to delete General notification %d: %s", message_id, exc)


def _payload_str(payload: dict, key: str) -> str:
    return str(payload.get(key) or "").strip()


def _display_id(payload: dict) -> str:
    return _payload_str(payload, "unique_id") or _payload_str(payload, "ticket_id")


async def on_assigned_on_create(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    from .work_schedule import is_work_time
    # If this ticket is assigned to our operator — they have a personal topic, no General needed
    if _is_our_operator(payload):
        return
    owner_name = _payload_str(payload, "owner_name")
    department = _payload_str(payload, "department")
    logger.info(
        "general_channel.on_assigned_on_create: ticket=%s owner_name=%r department=%r target_dept=%r",
        _payload_str(payload, "ticket_id"), owner_name, department, config.unassigned_department,
    )
    if not _is_unassigned(
        owner_name=owner_name,
        department=department,
        target_dept=config.unassigned_department,
    ):
        return
    ticket_id = _payload_str(payload, "ticket_id")
    existing = await db.get_general_message(ticket_id)
    if existing:
        return
    if not is_work_time():
        await db.save_pending_general(
            ticket_id=ticket_id,
            display_id=_display_id(payload),
            ticket_name=_payload_str(payload, "ticket_name"),
            link=_payload_str(payload, "link"),
        )
        _overnight_pending_ids.add(ticket_id)
        logger.info("Queued General notification for ticket %s (outside work hours)", ticket_id)
        return
    text = _format_general_message(
        display_id=_display_id(payload),
        ticket_name=_payload_str(payload, "ticket_name"),
        link=_payload_str(payload, "link"),
    )
    message_id = await _send(bot, text, ticket_id)
    if message_id:
        await db.save_general_message(ticket_id, message_id, _payload_str(payload, "ticket_name"))
        logger.info("Posted General notification for ticket %s (msg_id=%d)", ticket_id, message_id)


async def on_owner_changed(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    from .work_schedule import is_work_time
    ticket_id = _payload_str(payload, "ticket_id")
    owner_name = _payload_str(payload, "owner_name")
    department = _payload_str(payload, "department")
    logger.info(
        "general_channel.on_owner_changed: ticket=%s owner_name=%r department=%r target_dept=%r",
        ticket_id, owner_name, department, config.unassigned_department,
    )

    # If ticket is now assigned to our operator — treat as assigned (they get a personal topic)
    is_now_unassigned = (
        not _is_our_operator(payload)
        and _is_unassigned(
            owner_name=owner_name,
            department=_payload_str(payload, "department"),
            target_dept=config.unassigned_department,
        )
    )

    if not is_work_time():
        name = owner_name.strip().lower()
        has_real_owner = bool(name) and not any(m in name for m in _UNASSIGNED_MARKERS)
        if has_real_owner:
            await db.delete_pending_general(ticket_id)
            logger.info(
                "Removed ticket %s from pending General (assigned to %s outside work hours)",
                ticket_id, owner_name,
            )
        elif is_now_unassigned:
            existing_posted = await db.get_general_message(ticket_id)
            if not existing_posted:
                await db.save_pending_general(
                    ticket_id=ticket_id,
                    display_id=_display_id(payload),
                    ticket_name=_payload_str(payload, "ticket_name"),
                    link=_payload_str(payload, "link"),
                )
                _overnight_pending_ids.add(ticket_id)
                logger.info(
                    "Queued General notification for ticket %s on re-unassign (outside work hours)",
                    ticket_id,
                )
        return

    existing = await db.get_general_message(ticket_id)

    if is_now_unassigned:
        if existing:
            return  # already posted
        text = _format_general_message(
            display_id=_display_id(payload),
            ticket_name=_payload_str(payload, "ticket_name"),
            link=_payload_str(payload, "link"),
        )
        message_id = await _send(bot, text, ticket_id)
        if message_id:
            await db.save_general_message(ticket_id, message_id, _payload_str(payload, "ticket_name"))
            logger.info("Posted General notification on re-unassign for ticket %s", ticket_id)
    else:
        if existing is None:
            return
        # Only delete if ticket is confirmed assigned to a real person.
        # HDE sometimes sends owner_changed with empty department even when
        # the ticket IS in the target dept — in that case owner_name is also
        # empty and we cannot confirm the assignment, so keep the notification.
        name = owner_name.strip().lower()
        has_real_owner = bool(name) and not any(m in name for m in _UNASSIGNED_MARKERS)
        if not has_real_owner:
            logger.info(
                "Keeping General notification for ticket %s (owner_name empty or unassigned in payload)",
                ticket_id,
            )
            return
        await _delete(bot, existing["message_id"])
        await db.delete_general_message(ticket_id)
        logger.info("Deleted General notification for ticket %s (assigned to %s)", ticket_id, owner_name)


async def on_ticket_updated(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    from .work_schedule import is_work_time
    ticket_id = _payload_str(payload, "ticket_id")
    owner_name = _payload_str(payload, "owner_name")
    department = _payload_str(payload, "department")
    logger.info(
        "general_channel.on_ticket_updated: ticket=%s owner_name=%r department=%r target_dept=%r",
        ticket_id, owner_name, department, config.unassigned_department,
    )

    # New unassigned ticket arriving via ticket_updated (HDE doesn't send assigned_on_create)
    if not _is_our_operator(payload) and _is_unassigned(owner_name, department, config.unassigned_department):
        existing_msg = await db.get_general_message(ticket_id)
        if existing_msg is None:
            if not is_work_time():
                await db.save_pending_general(
                    ticket_id=ticket_id,
                    display_id=_display_id(payload),
                    ticket_name=_payload_str(payload, "ticket_name"),
                    link=_payload_str(payload, "link"),
                )
                _overnight_pending_ids.add(ticket_id)
                logger.info("Queued General notification for ticket %s (ticket_updated, outside work hours)", ticket_id)
            else:
                text = _format_general_message(
                    display_id=_display_id(payload),
                    ticket_name=_payload_str(payload, "ticket_name"),
                    link=_payload_str(payload, "link"),
                )
                message_id = await _send(bot, text, ticket_id)
                if message_id:
                    await db.save_general_message(ticket_id, message_id, _payload_str(payload, "ticket_name"))
                    logger.info("Posted General notification for ticket %s (via ticket_updated)", ticket_id)
            return

    existing = await db.get_general_message(ticket_id)
    if existing is None:
        return
    new_name = _payload_str(payload, "ticket_name")
    if existing["ticket_name"] == new_name:
        return
    text = _format_general_message(
        display_id=_display_id(payload),
        ticket_name=new_name,
        link=_payload_str(payload, "link"),
    )
    await _edit(bot, existing["message_id"], text, ticket_id)
    await db.save_general_message(ticket_id, existing["message_id"], new_name)
    logger.info("Edited General notification for ticket %s", ticket_id)


async def on_ticket_closed(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    ticket_id = _payload_str(payload, "ticket_id")
    await db.delete_pending_general(ticket_id)
    existing = await db.get_general_message(ticket_id)
    if existing is None:
        return
    await _delete(bot, existing["message_id"])
    await db.delete_general_message(ticket_id)
    logger.info("Deleted General notification on close for ticket %s", ticket_id)


async def flush_overnight_general(bot: Bot) -> None:
    """Send overnight summary to personal chat, then post unassigned tickets to General."""
    global _overnight_pending_ids
    if config.general_topic_id is None:
        return
    pending = await db.list_pending_general()

    # Stats: use in-memory tracker if available, otherwise fall back to pending count
    total_received = max(len(_overnight_pending_ids), len(pending))
    unassigned_count = len(pending)
    assigned_overnight = total_received - unassigned_count

    summary = _format_overnight_summary(total_received, assigned_overnight, unassigned_count)
    try:
        await bot.send_message(config.personal_chat_id, summary, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to send overnight summary: %s", exc)

    _overnight_pending_ids.clear()

    if not pending:
        return
    logger.info("Flushing %d overnight General notifications", len(pending))
    for row in pending:
        ticket_id = row["ticket_id"]
        existing = await db.get_general_message(ticket_id)
        if existing:
            await db.delete_pending_general(ticket_id)
            continue
        text = _format_general_message(
            display_id=row["display_id"],
            ticket_name=row["ticket_name"],
            link=row["link"],
        )
        message_id = await _send(bot, text, ticket_id)
        if message_id:
            await db.save_general_message(ticket_id, message_id, row["ticket_name"])
            await db.delete_pending_general(ticket_id)
            logger.info("Flushed General notification for ticket %s (msg_id=%d)", ticket_id, message_id)
        else:
            logger.error("Failed to flush General notification for ticket %s — kept in pending", ticket_id)
