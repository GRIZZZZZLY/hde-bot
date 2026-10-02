"""
Notifications for unassigned tickets in the General Telegram topic.

Enabled only when GENERAL_TOPIC_ID is set in config.
"""
from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import db, operators
from .config import config

# Prevents duplicate General notifications when ticket_updated + owner_changed arrive concurrently
_currently_posting: set[str] = set()

def _is_our_operator(payload: dict) -> bool:
    """Return True if the ticket is owned by one of our engineers (they have a topic for it)."""
    return operators.by_owner(
        _payload_str(payload, "owner_id"),
        _payload_str(payload, "owner_name"),
    ) is not None

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


def _format_general_message(display_id: str, ticket_name: str, link: str) -> str:
    parts = [
        "🆕 <b>Неприсвоенный тикет</b>\n",
        f"#{display_id} — {ticket_name}",
    ]
    if link:
        parts.append(f'\n<a href="{link}">Открыть в HDE</a>')
    return "\n".join(parts)


TAKE_HOURS_SETTING = "take_busy_hours"
_DEFAULT_TAKE_HOURS = "1 1-2 2 4"
_BUSY_OPTION = re.compile(r"(\d{1,2})(?:-(\d{1,2}))?")


def is_busy_option(mode: str) -> bool:
    """«2» or «1-2»: hours, 1..72, range ascending."""
    m = _BUSY_OPTION.fullmatch(mode)
    if not m:
        return False
    low, high = int(m[1]), int(m[2] or m[1])
    return 1 <= low <= high <= 72 and (m[2] is None or low < high)


def parse_busy_options(text: str) -> list[str] | None:
    """«1 1-2 2 4» → ['1', '1-2', '2', '4']; None if any option is invalid or the count is off."""
    options = text.replace(",", " ").replace("–", "-").split()
    if not 1 <= len(options) <= 6 or not all(is_busy_option(o) for o in options):
        return None
    return options


async def busy_options() -> list[str]:
    raw = await db.get_setting(TAKE_HOURS_SETTING, _DEFAULT_TAKE_HOURS)
    return parse_busy_options(raw) or parse_busy_options(_DEFAULT_TAKE_HOURS)


def hours_label(mode: str) -> str:
    """«1-2» → «1–2» for display."""
    return mode.replace("-", "–")


async def _take_keyboard(ticket_id: str) -> InlineKeyboardMarkup:
    """take:{id}:now → «ready» greeting, take:{id}:{hours} → «busy» greeting.

    Hour options come from bot_settings (/taketimes). Plain take:{id} (messages
    posted before the greeting buttons) still assigns without writing to the
    client — see cb_take_ticket.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤙 Беру сейчас", callback_data=f"take:{ticket_id}:now")],
        [
            InlineKeyboardButton(text=f"⏳ {hours_label(h)} ч", callback_data=f"take:{ticket_id}:{h}")
            for h in await busy_options()
        ],
    ])


def _hours_genitive(hours: int) -> str:
    """«в течение 1 часа / 21 часа», «в течение 2 / 4 / 11 часов»."""
    return "часа" if hours % 10 == 1 and hours % 100 != 11 else "часов"


def take_greeting(first_name: str, mode: str) -> str:
    """First public message to the client when an engineer takes the ticket from General."""
    if mode == "now":
        return (
            f"Здравствуйте, меня зовут {first_name}, инженер по оборудованию. "
            "Изучаю информацию по вашему обращению, вернусь через 5 минут."
        )
    upper = int(mode.split("-")[-1])  # «в течение 1–2 часов»: the word agrees with the upper bound
    return (
        f"Здравствуйте! Меня зовут {first_name}, инженер по оборудованию. "
        "Сейчас у нас большое количество обращений, поэтому решение вашего запроса "
        "займёт немного больше времени. "
        f"Вернусь к вам с ответом в течение {hours_label(mode)} {_hours_genitive(upper)}."
    )


def _general_chats() -> list[int]:
    """Groups of the engineers at work now get the General notification; the others
    get it from the periodic reconcile once their working hours start."""
    from .work_schedule import is_work_time_for
    return list(dict.fromkeys(o.chat_id for o in operators.all_operators() if is_work_time_for(o)))


async def _send(bot: Bot, text: str, ticket_id: str, ticket_name: str) -> int:
    """Post the notification to the General thread of every group that does not have it yet.

    Saves each posted message and returns how many groups got it.
    """
    assert config.general_topic_id is not None
    have = {row["chat_id"] for row in await db.list_general_messages_for(ticket_id)}
    posted = 0
    for chat_id in _general_chats():
        if chat_id in have:
            continue
        try:
            # thread_id=1 is the General topic in forum groups — Telegram may reject it
            # when the group was created without explicit topics; omit it so the message
            # falls through to the main (General) thread automatically.
            kwargs: dict = dict(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=await _take_keyboard(ticket_id),
            )
            if config.general_topic_id != 1:
                kwargs["message_thread_id"] = config.general_topic_id
            msg = await bot.send_message(**kwargs)
        except TelegramAPIError as exc:
            logger.error("Failed to send General notification to chat %s: %s", chat_id, exc)
            continue
        await db.save_general_message(ticket_id, msg.message_id, ticket_name, chat_id)
        posted += 1
    return posted


async def _edit_all(bot: Bot, ticket_id: str, text: str, ticket_name: str) -> None:
    assert config.general_topic_id is not None
    for row in await db.list_general_messages_for(ticket_id):
        try:
            await bot.edit_message_text(
                chat_id=row["chat_id"],
                message_id=row["message_id"],
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=await _take_keyboard(ticket_id),
            )
        except TelegramAPIError as exc:
            logger.error("Failed to edit General notification %d: %s", row["message_id"], exc)
        await db.save_general_message(ticket_id, row["message_id"], ticket_name, row["chat_id"])


async def _delete_all(bot: Bot, ticket_id: str) -> None:
    """Remove the ticket's General notification from every group."""
    for row in await db.list_general_messages_for(ticket_id):
        try:
            await bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
        except TelegramAPIError as exc:
            logger.error("Failed to delete General notification %d: %s", row["message_id"], exc)
    await db.delete_general_message(ticket_id)


def _payload_str(payload: dict, key: str) -> str:
    return str(payload.get(key) or "").strip()


def _display_id(payload: dict) -> str:
    return _payload_str(payload, "unique_id") or _payload_str(payload, "ticket_id")


async def on_assigned_on_create(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    from .work_schedule import anyone_at_work as is_work_time
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
        logger.info("Queued General notification for ticket %s (outside work hours)", ticket_id)
        return
    text = _format_general_message(
        display_id=_display_id(payload),
        ticket_name=_payload_str(payload, "ticket_name"),
        link=_payload_str(payload, "link"),
    )
    if await _send(bot, text, ticket_id, _payload_str(payload, "ticket_name")):
        logger.info("Posted General notification for ticket %s", ticket_id)


async def on_owner_changed(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    from .work_schedule import anyone_at_work as is_work_time
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
                logger.info(
                    "Queued General notification for ticket %s on re-unassign (outside work hours)",
                    ticket_id,
                )
        return

    existing = await db.get_general_message(ticket_id)

    if is_now_unassigned:
        if existing:
            return  # already posted
        if ticket_id in _currently_posting:
            return  # being handled by concurrent ticket_updated
        _currently_posting.add(ticket_id)
        try:
            existing_recheck = await db.get_general_message(ticket_id)
            if existing_recheck is None:
                text = _format_general_message(
                    display_id=_display_id(payload),
                    ticket_name=_payload_str(payload, "ticket_name"),
                    link=_payload_str(payload, "link"),
                )
                if await _send(bot, text, ticket_id, _payload_str(payload, "ticket_name")):
                    logger.info("Posted General notification on re-unassign for ticket %s", ticket_id)
        finally:
            _currently_posting.discard(ticket_id)
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
        await _delete_all(bot, ticket_id)
        logger.info("Deleted General notification for ticket %s (assigned to %s)", ticket_id, owner_name)


async def on_ticket_updated(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    from .work_schedule import anyone_at_work as is_work_time
    ticket_id = _payload_str(payload, "ticket_id")
    owner_name = _payload_str(payload, "owner_name")
    department = _payload_str(payload, "department")
    logger.info(
        "general_channel.on_ticket_updated: ticket=%s owner_name=%r department=%r target_dept=%r",
        ticket_id, owner_name, department, config.unassigned_department,
    )

    # New unassigned ticket arriving via ticket_updated (HDE doesn't send assigned_on_create)
    if not _is_our_operator(payload) and _is_unassigned(owner_name, department, config.unassigned_department):
        posted = await db.get_general_message(ticket_id)
        if posted is not None:
            await _rename_if_changed(bot, payload, posted)
            return
        if ticket_id not in _currently_posting:
            _currently_posting.add(ticket_id)
            try:
                existing_msg = await db.get_general_message(ticket_id)
                if existing_msg is None:
                    if not is_work_time():
                        await db.save_pending_general(
                            ticket_id=ticket_id,
                            display_id=_display_id(payload),
                            ticket_name=_payload_str(payload, "ticket_name"),
                            link=_payload_str(payload, "link"),
                        )
                        logger.info("Queued General notification for ticket %s (ticket_updated, outside work hours)", ticket_id)
                    else:
                        text = _format_general_message(
                            display_id=_display_id(payload),
                            ticket_name=_payload_str(payload, "ticket_name"),
                            link=_payload_str(payload, "link"),
                        )
                        if await _send(bot, text, ticket_id, _payload_str(payload, "ticket_name")):
                            logger.info("Posted General notification for ticket %s (via ticket_updated)", ticket_id)
            finally:
                _currently_posting.discard(ticket_id)
        return

    existing = await db.get_general_message(ticket_id)
    if existing is None:
        return

    # Ticket has a General message but is no longer unassigned — usually means a
    # colleague self-assigned it directly in HDE without an owner_changed event.
    # Mirror on_owner_changed: only delete if a real owner is confirmed in this
    # payload, otherwise keep the notification (HDE sometimes sends ticket_updated
    # with an empty owner_name even when the ticket is still in target dept).
    name = owner_name.strip().lower()
    has_real_owner = bool(name) and not any(m in name for m in _UNASSIGNED_MARKERS)
    if has_real_owner or _is_our_operator(payload):
        await _delete_all(bot, ticket_id)
        logger.info(
            "Deleted General notification on ticket_updated for ticket %s (assigned to %r)",
            ticket_id, owner_name or "our operator",
        )
        return

    await _rename_if_changed(bot, payload, existing)


async def _rename_if_changed(bot: Bot, payload: dict, posted: dict) -> None:
    """The ticket was renamed in HDE: update its General post in every group."""
    ticket_id = _payload_str(payload, "ticket_id")
    new_name = _payload_str(payload, "ticket_name")
    if not new_name or posted["ticket_name"] == new_name:
        return
    text = _format_general_message(
        display_id=_display_id(payload),
        ticket_name=new_name,
        link=_payload_str(payload, "link"),
    )
    await _edit_all(bot, ticket_id, text, new_name)
    logger.info("Edited General notification for ticket %s", ticket_id)


async def on_ticket_closed(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    ticket_id = _payload_str(payload, "ticket_id")
    await db.delete_pending_general(ticket_id)
    if await db.get_general_message(ticket_id) is None:
        return
    await _delete_all(bot, ticket_id)
    logger.info("Deleted General notification on close for ticket %s", ticket_id)


async def reconcile_with_hde(bot: Bot) -> None:
    """Sync General-topic state with HDE: remove stale messages, post missing unassigned tickets.

    Used both by the morning flush and by the periodic in-hours reconcile loop.
    """
    if config.general_topic_id is None:
        return
    from .hde_api import HDEApiClient, HDEApiError
    try:
        client = HDEApiClient()
    except HDEApiError as exc:
        logger.warning("HDE reconcile skipped — API not configured: %s", exc)
        return

    try:
        unassigned = await client.get_unassigned_tickets(config.unassigned_department)
    except HDEApiError as exc:
        logger.warning("HDE reconcile failed to fetch unassigned: %s", exc)
        return
    except Exception as exc:
        logger.exception("HDE reconcile fetch crashed: %s", exc)
        return

    unassigned_by_id = {t.ticket_id: t for t in unassigned}

    # 1) Remove stale General messages (ticket no longer unassigned/open in HDE)
    stale_ids = {row["ticket_id"] for row in await db.list_general_messages()} - set(unassigned_by_id)
    for ticket_id in stale_ids:
        if ticket_id in _currently_posting:
            continue  # webhook is mid-flight on this ticket
        await _delete_all(bot, ticket_id)
        logger.info("Reconcile: removed stale General message for ticket %s", ticket_id)

    # 2) Post missing General messages (unassigned in HDE but not yet posted in some group).
    #    _send skips the groups that already have it, so a newly added engineer catches up.
    for ticket in unassigned:
        if ticket.ticket_id in _currently_posting:
            continue  # webhook is mid-flight on this ticket
        _currently_posting.add(ticket.ticket_id)
        try:
            text = _format_general_message(
                display_id=ticket.unique_id,
                ticket_name=ticket.title,
                link=ticket.hde_link,
            )
            if await _send(bot, text, ticket.ticket_id, ticket.title):
                # Drop from pending if it was queued — we just posted it via reconcile
                await db.delete_pending_general(ticket.ticket_id)
                logger.info("Reconcile: posted missing General message for ticket %s", ticket.ticket_id)
        finally:
            _currently_posting.discard(ticket.ticket_id)


async def flush_overnight_general(bot: Bot) -> None:
    """Reconcile General-topic with HDE, post pending overnight tickets."""
    if config.general_topic_id is None:
        return

    pending = await db.list_pending_general()

    # 1) Flush queued overnight tickets — post them to General
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
        if await _send(bot, text, ticket_id, row["ticket_name"]):
            await db.delete_pending_general(ticket_id)
            logger.info("Flushed General notification for ticket %s", ticket_id)
        else:
            logger.error("Failed to flush General notification for ticket %s — kept in pending", ticket_id)

    # 2) Reconcile with HDE — remove phantoms, add tickets the bot missed
    await reconcile_with_hde(bot)
