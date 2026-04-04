from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config
from .time_utils import to_storage, utcnow
from .topic_manager import delete_pending_topic, send_pre_sla_alert

logger = logging.getLogger(__name__)

_last_digest_date: Optional[str] = None  # "YYYY-MM-DD" UTC date


async def _maybe_send_digest(bot: Bot) -> None:
    global _last_digest_date
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour != config.digest_send_hour_utc:
        return
    if _last_digest_date == today:
        return
    _last_digest_date = today
    from .digest import send_morning_digest
    await send_morning_digest(bot)


async def process_scheduled_actions(bot: Bot) -> None:
    now_value = to_storage(utcnow())

    for record in await db.list_due_pre_sla(now_value):
        try:
            await send_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error("Failed to send pre-SLA alert for ticket %s: %s", record.ticket_id, exc)

    for record in await db.list_due_deletions(now_value):
        await delete_pending_topic(bot, record)

    await _maybe_send_digest(bot)


async def run_scheduler(bot: Bot, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await process_scheduled_actions(bot)
        except Exception as exc:
            logger.exception("Scheduler loop failed: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.scheduler_interval_seconds)
        except asyncio.TimeoutError:
            continue
