"""Pre-SLA alert logic, reassurance messages and SLA scheduling helpers.

Mechanically extracted from bot.topic_manager (pure move, no behavior change).
Shared state and test patch-points (config, db, utcnow, ...) are accessed
late-bound through the bot.topic_manager module object so monkeypatches on
bot.topic_manager keep working.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .formatter import (
    format_pre_sla_alert_general,
    format_pre_sla_alert_topic,
)
from .time_utils import parse_datetime, to_storage

logger = logging.getLogger(__name__)


def _parse_minutes(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _calculate_pre_sla_notify_at(payload: dict) -> str:
    """Schedule pre-SLA alert based on reply SLA (default_reply_sla_minutes from last post).
    If HDE reports a tighter deadline (sla_remaining_minutes < default_reply_sla_minutes),
    that deadline wins instead.
    """
    from . import topic_manager as _tm
    now = _tm.utcnow()
    warning_minutes = max(_tm.config.pre_sla_warning_minutes, 0)
    total_sla_minutes = max(_tm.config.default_reply_sla_minutes, 0)

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


async def _schedule_pre_sla(ticket_id: str, payload: dict, last_client_reply_at: str) -> None:
    from . import topic_manager as _tm
    notify_at = _calculate_pre_sla_notify_at(payload)
    logger.info(
        "PRESLA-DIAG schedule ticket=%s notify_at=%s lcr=%s",
        ticket_id, notify_at, last_client_reply_at,
    )
    await _tm.db.update_topic(
        ticket_id,
        last_client_reply_at=last_client_reply_at,
        pre_sla_notify_at=notify_at,
        pre_sla_sent_at=None,
        # Флаг автоответа — на цикл ожидания, а не на тикет: без сброса второе
        # неотвеченное сообщение в том же тикете автоответа уже не получало.
        reassurance_sent_at=None,
    )


def _pre_sla_minutes_left(record: "db.TicketTopic") -> int:
    """Вычисляет целые минуты до SLA (floor, как в HDE). 0 = меньше 1 минуты."""
    import math
    from . import topic_manager as _tm
    deadline = parse_datetime(record.pre_sla_notify_at)
    if deadline is None:
        return _tm.config.pre_sla_warning_minutes
    sla_deadline = deadline + timedelta(minutes=_tm.config.pre_sla_warning_minutes)
    remaining = (sla_deadline - _tm.utcnow()).total_seconds() / 60
    return max(0, math.floor(remaining))


def _pre_sla_destination(record: "db.TicketTopic") -> tuple[int, int | None]:
    """Возвращает (chat_id, thread_id) для pre-SLA сообщения.

    Если тикет назначен — топик тикета.
    Если нет исполнителя — General (general_topic_id).
    Fallback: если General не настроен, шлём в топик тикета.
    """
    from . import topic_manager as _tm
    has_owner = bool(record.owner_id.strip())
    if has_owner:
        return _tm.config.group_chat_id, record.topic_id
    if _tm.config.general_topic_id is not None:
        return _tm.config.group_chat_id, _tm.config.general_topic_id
    return _tm.config.group_chat_id, record.topic_id


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


async def send_reassurance_to_client(bot: Bot, record: db.TicketTopic) -> None:
    """Send reassurance post to client via HDE and notify topic. Idempotent via reassurance_sent_at."""
    from . import topic_manager as _tm
    from .hde_api import HDEApiClient, HDEApiError
    try:
        client = HDEApiClient()
        await client.add_post(record.ticket_id, _tm.config.reassurance_text)
    except HDEApiError as exc:
        logger.warning("Reassurance post failed for ticket %s: %s", record.ticket_id, exc)
        return  # don't mark sent — retry next tick

    await _tm.db.update_topic(record.ticket_id, reassurance_sent_at=to_storage(_tm.utcnow()))

    try:
        await bot.send_message(
            chat_id=_tm.config.group_chat_id,
            message_thread_id=record.topic_id,
            text=(
                "🤖 <b>Автоответ клиенту отправлен</b>\n"
                f"<i>{_tm.config.reassurance_text}</i>"
            ),
            parse_mode="HTML",
            disable_notification=True,
        )
    except TelegramAPIError as exc:
        logger.warning("Failed to notify topic about reassurance for %s: %s", record.ticket_id, exc)


async def send_pre_sla_alert(bot: Bot, record: db.TicketTopic) -> None:
    from . import topic_manager as _tm
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
    await _tm.db.mark_pre_sla_sent(record.ticket_id, message_id=msg.message_id)


async def update_pre_sla_alert(bot: Bot, record: db.TicketTopic) -> None:
    """Удаляет старое pre-SLA сообщение и присылает новое с актуальным счётчиком."""
    from . import topic_manager as _tm
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
    await _tm.db.mark_pre_sla_sent(record.ticket_id, message_id=msg.message_id)


async def _try_delete_pre_sla_message(bot: Bot, record: db.TicketTopic) -> None:
    """Удаляет pre-SLA сообщение из Telegram если оно было отправлено."""
    if not record.pre_sla_message_id:
        return
    chat_id, _ = _pre_sla_destination(record)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=record.pre_sla_message_id)
    except TelegramAPIError:
        pass
