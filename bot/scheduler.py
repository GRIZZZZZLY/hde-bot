from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .config import config
from .time_utils import to_storage, utcnow
from .topic_manager import delete_pending_topic, send_pre_sla_alert

logger = logging.getLogger(__name__)

_last_digest_date: Optional[str] = None   # "YYYY-MM-DD" UTC date
_last_report_date: Optional[str] = None   # "YYYY-MM-DD" UTC date — set when report runs
_report_button_sent: Optional[str] = None  # "YYYY-MM-DD" UTC date — set when button is sent
_ALLOWED_UPDATES = ["message", "callback_query"]


def mark_report_done_today() -> None:
    """Mark today's report as done so the auto-run scheduler skips it."""
    global _last_report_date
    _last_report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    import asyncio as _asyncio
    from datetime import date as _date
    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(db.mark_report_sent(_date.today()))
    except Exception:
        pass


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


async def _maybe_send_report_button(bot: Bot) -> None:
    """At REPORT_SEND_HOUR_UTC: send an inline button to personal chat."""
    global _report_button_sent
    from .reporting.runner import is_report_configured
    if not is_report_configured():
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    offer_hour = int(os.getenv("REPORT_SEND_HOUR_UTC", "6"))
    if now.hour != offer_hour:
        return
    if _report_button_sent == today or _last_report_date == today:
        return
    yesterday = now.date() - timedelta(days=1)
    if await db.is_report_sent(yesterday):
        return
    _report_button_sent = today
    yesterday = (now.date() - timedelta(days=1)).strftime("%d.%m.%Y")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"📊 Сформировать отчёт за {yesterday}",
            callback_data="report:run_yesterday",
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="report:cancel",
        ),
    ]])
    await bot.send_message(
        config.personal_chat_id,
        "⏰ <b>Напоминание:</b> пора сформировать ежедневный отчёт.\n\n"
        "Нажмите кнопку или подождите — через час отчёт сформируется автоматически.",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def _maybe_auto_run_report(bot: Bot) -> None:
    """At REPORT_SEND_HOUR_UTC + 1: auto-run if button was not pressed."""
    global _last_report_date
    from .reporting.runner import is_report_configured, run_report
    if not is_report_configured():
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    offer_hour = int(os.getenv("REPORT_SEND_HOUR_UTC", "6"))
    if now.hour != offer_hour + 1:
        return
    if _last_report_date == today:
        return
    yesterday = now.date() - timedelta(days=1)
    if await db.is_report_sent(yesterday):
        _last_report_date = today
        return
    _last_report_date = today
    try:
        result = await run_report()
        await db.mark_report_sent(yesterday)
        await bot.send_message(config.personal_chat_id, result, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Daily report failed: %s", exc)
        await bot.send_message(
            config.personal_chat_id,
            f"❌ <b>Ошибка при формировании отчёта:</b>\n<code>{exc}</code>",
            parse_mode="HTML",
        )



async def process_scheduled_actions(bot: Bot) -> None:
    from .work_schedule import is_work_day, is_work_time, was_yesterday_work_day

    # Digest fires on any work day (morning briefing)
    if is_work_day():
        await _maybe_send_digest(bot)

    # Report only makes sense if yesterday was a work day — otherwise
    # the operator had no activity and HDE won't list them at all.
    if was_yesterday_work_day():
        await _maybe_send_report_button(bot)
        await _maybe_auto_run_report(bot)

    # Pre-SLA and pending deletions require both work day AND work hours
    if not is_work_time():
        return

    now_value = to_storage(utcnow())

    for record in await db.list_due_pre_sla(now_value):
        try:
            await send_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error("Failed to send pre-SLA alert for ticket %s: %s", record.ticket_id, exc)

    for record in await db.list_due_deletions(now_value):
        await delete_pending_topic(bot, record)


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
