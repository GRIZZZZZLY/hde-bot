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
from .time_utils import to_storage, utcnow, parse_datetime
from .topic_manager import delete_pending_topic, send_pre_sla_alert, update_pre_sla_alert

logger = logging.getLogger(__name__)

_last_digest_date: Optional[str] = None         # "YYYY-MM-DD" UTC date
_last_general_flush_date: Optional[str] = None  # "YYYY-MM-DD" UTC date
_last_report_date: Optional[str] = None         # "YYYY-MM-DD" UTC date — set when report runs
_report_button_sent: Optional[str] = None  # "YYYY-MM-DD" UTC date — set when button is sent
_last_knowledge_expiry_date: Optional[str] = None  # "YYYY-MM-DD" UTC date — set when weekly expiry runs
_last_optimization_date: Optional[str] = None  # "YYYY-MM-DD" UTC date — set when nightly optimizer runs
_last_media_gc_hour: Optional[str] = None  # "YYYY-MM-DD HH" — set when hourly media GC runs
_ALLOWED_UPDATES = ["message", "callback_query"]

KNOWLEDGE_EXPIRY_DAYS = 180


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


async def _maybe_flush_general(bot: Bot) -> None:
    global _last_general_flush_date
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour != config.digest_send_hour_utc:
        return
    if _last_general_flush_date == today:
        return
    _last_general_flush_date = today
    from .general_channel import flush_overnight_general
    try:
        await flush_overnight_general(bot)
    except Exception as exc:
        logger.warning("Overnight General flush failed: %s", exc)


async def _maybe_send_report_button(bot: Bot) -> None:
    """At REPORT_SEND_HOUR_UTC: send an inline button to personal chat."""
    global _report_button_sent
    from .reporting.runner import is_report_configured
    from .work_schedule import last_work_day
    if not is_report_configured():
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    offer_hour = int(os.getenv("REPORT_SEND_HOUR_UTC", "6"))
    if now.hour != offer_hour:
        return
    if _report_button_sent == today or _last_report_date == today:
        return
    last_wd = last_work_day()
    if await db.is_report_sent(last_wd):
        logger.info("Report for %s already sent, skipping button", last_wd)
        return
    _report_button_sent = today
    last_wd_str = last_wd.strftime("%d.%m.%Y")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"📊 Сформировать отчёт за {last_wd_str}",
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
    from .work_schedule import last_work_day
    if not is_report_configured():
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    offer_hour = int(os.getenv("REPORT_SEND_HOUR_UTC", "6"))
    if now.hour != offer_hour + 1:
        return
    if _last_report_date == today:
        return
    last_wd = last_work_day()
    if await db.is_report_sent(last_wd):
        _last_report_date = today
        return
    _last_report_date = today
    try:
        result = await run_report(last_wd)
        await db.mark_report_sent(last_wd)
        await bot.send_message(config.personal_chat_id, result, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Daily report failed: %s", exc)
        await bot.send_message(
            config.personal_chat_id,
            f"❌ <b>Ошибка при формировании отчёта:</b>\n<code>{exc}</code>",
            parse_mode="HTML",
        )



async def process_scheduled_actions(bot: Bot) -> None:
    from .work_schedule import is_work_day, is_work_time, last_work_day, was_yesterday_work_day

    # Digest + overnight General flush fire on any work day at digest hour
    if is_work_day():
        await _maybe_send_digest(bot)
        await _maybe_flush_general(bot)

    # Report button fires on any work day — uses last_work_day() so Monday
    # correctly prompts for Friday's report, not Sunday.
    if is_work_day():
        await _maybe_send_report_button(bot)
        await _maybe_auto_run_report(bot)

    # Hourly media cache GC — remove topic_media_cache rows older than 1h
    global _last_media_gc_hour
    now = datetime.now(timezone.utc)
    gc_key = now.strftime("%Y-%m-%d %H")
    if _last_media_gc_hour != gc_key:
        _last_media_gc_hour = gc_key
        try:
            deleted = await db.gc_stale_media_cache(hours=1)
            if deleted:
                logger.info("Media cache GC: removed %d stale rows", deleted)
        except Exception as exc:
            logger.warning("Media cache GC failed: %s", exc)

    # Weekly knowledge expiry (Sunday 00:xx UTC)
    global _last_knowledge_expiry_date
    today = now.strftime("%Y-%m-%d")
    if now.weekday() == 6 and now.hour == 0:
        if _last_knowledge_expiry_date != today:
            _last_knowledge_expiry_date = today
            try:
                marked = await db.expire_stale_knowledge(KNOWLEDGE_EXPIRY_DAYS)
                if marked:
                    logger.info("Weekly expiry: marked %d stale knowledge items as expired", marked)
            except Exception as exc:
                logger.warning("Weekly knowledge expiry failed: %s", exc)

    # Nightly prompt optimization — daily at 23:00 UTC (02:00 MSK)
    if now.hour == 23 and now.minute < 1:
        global _last_optimization_date
        if _last_optimization_date != today:
            _last_optimization_date = today
            try:
                from .optimizer.agent import run_optimizer
                asyncio.create_task(run_optimizer(bot))
                logger.info("Scheduled prompt optimizer for tonight")
            except Exception as exc:
                logger.warning("Failed to schedule optimizer: %s", exc)

    # Pre-SLA and pending deletions require both work day AND work hours
    if not is_work_time():
        return

    now_value = to_storage(utcnow())

    for record in await db.list_due_pre_sla(now_value):
        try:
            await send_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error("Failed to send pre-SLA alert for ticket %s: %s", record.ticket_id, exc)

    for record in await db.list_active_pre_sla():
        if record.pre_sla_sent_at:
            last_update = parse_datetime(record.pre_sla_sent_at)
            if last_update and (utcnow() - last_update).total_seconds() < 55:
                continue
        try:
            await update_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error(
                "Failed to update pre-SLA countdown for ticket %s: %s",
                record.ticket_id, exc,
            )

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
