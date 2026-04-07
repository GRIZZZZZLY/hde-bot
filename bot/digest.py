# digest.py
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config
from .formatter import format_morning_digest
from .hde_api import HDEApiClient, HDEApiError
from .time_utils import to_storage

logger = logging.getLogger(__name__)


def _night_window(send_hour_utc: int, night_start_hour_utc: int) -> tuple[datetime, datetime]:
    """Calculate the overnight window: night_start yesterday → send_hour today (UTC)."""
    now = datetime.now(timezone.utc)
    night_end = now.replace(hour=send_hour_utc, minute=0, second=0, microsecond=0)
    night_start = night_end.replace(hour=night_start_hour_utc)
    if night_start >= night_end:
        night_start -= timedelta(days=1)
    return night_start, night_end


def _label(dt: datetime) -> str:
    """Format datetime as 'HH:MM DD.MM' in MSK (+3)."""
    msk = dt + timedelta(hours=3)
    return msk.strftime("%H:%M %d.%m")


def _sla_sort_key(ticket) -> str:
    sla = getattr(ticket, "sla_date", None)
    if not sla or sla == "null":
        return "9999-99-99"
    try:
        parsed = datetime.strptime(sla, "%d.%m.%Y %H:%M")
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "9999-99-99"


async def send_morning_digest(bot: Bot) -> None:
    night_start, night_end = _night_window(
        config.digest_send_hour_utc,
        config.digest_night_start_hour_utc,
    )

    assigned = await db.list_overnight_assigned(
        to_storage(night_start),
        to_storage(night_end),
    )

    active_topics = await db.list_active_topics()
    total_open = len(active_topics)

    open_tickets_with_sla: list = []
    try:
        client = HDEApiClient()
        all_tickets = await client.get_my_open_tickets()
        open_tickets_with_sla = sorted(all_tickets, key=_sla_sort_key)
    except HDEApiError as exc:
        logger.warning("Failed to fetch tickets for digest SLA: %s", exc)

    unassigned_equipment_count = await db.count_general_messages()

    text = format_morning_digest(
        night_start_label=_label(night_start),
        night_end_label=_label(night_end),
        assigned_tickets=assigned,
        total_open=total_open,
        open_tickets_with_sla=open_tickets_with_sla,
        unassigned_equipment_count=unassigned_equipment_count,
    )

    try:
        await bot.send_message(
            chat_id=config.group_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Morning digest sent: %d assigned overnight, %d open", len(assigned), total_open)
    except TelegramAPIError as exc:
        logger.error("Failed to send morning digest: %s", exc)
