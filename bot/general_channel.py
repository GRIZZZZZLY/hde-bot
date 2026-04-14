"""
Notifications for unassigned tickets in the General Telegram topic.

Enabled only when GENERAL_TOPIC_ID is set in config.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .config import config

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
    if target_dept and department.strip().lower() != target_dept.strip().lower():
        return False
    return True


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
    if not is_work_time():
        return
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
    if not is_work_time():
        return
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
    ticket_id = _payload_str(payload, "ticket_id")
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
    existing = await db.get_general_message(ticket_id)
    if existing is None:
        return
    await _delete(bot, existing["message_id"])
    await db.delete_general_message(ticket_id)
    logger.info("Deleted General notification on close for ticket %s", ticket_id)
