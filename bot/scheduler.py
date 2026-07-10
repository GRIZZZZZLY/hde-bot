from __future__ import annotations

import asyncio
import logging
import os
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .config import config
from .time_utils import to_storage, utcnow, parse_datetime
from .topic_manager import (
    delete_pending_topic,
    send_pre_sla_alert,
    update_pre_sla_alert,
    _hde_staff_replied_since,
    _try_delete_pre_sla_message,
    send_reassurance_to_client,
)

logger = logging.getLogger(__name__)

_MSK = zoneinfo.ZoneInfo("Europe/Moscow")

# Daily report schedule (MSK). Mon–Thu get the morning reminder; Thu also gets
# an evening auto-run; Sun gets the weekly summary instead of a reminder.
_REPORT_MORNING_HM = (8, 30)
_REPORT_AUTORUN_HM = (9, 0)
_THU_EVENING_HM = (19, 0)
_WEEKLY_SUMMARY_HM = (8, 30)
_TRIGGER_WINDOW_MIN = 5  # fire if current MSK time is within [target, target+window)

_last_digest_date: Optional[str] = None         # "YYYY-MM-DD" UTC date
_last_general_flush_date: Optional[str] = None  # "YYYY-MM-DD" UTC date
_last_general_reconcile_at: Optional[datetime] = None  # last in-hours reconcile time
_GENERAL_RECONCILE_INTERVAL_SEC = 7 * 60
_last_report_date: Optional[str] = None         # "YYYY-MM-DD" UTC date — set when report runs
_report_button_sent: Optional[str] = None  # "YYYY-MM-DD" UTC date — set when button is sent
_thursday_evening_done: Optional[str] = None  # "YYYY-MM-DD" MSK date — Thu evening auto-run flag
_weekly_summary_done: Optional[str] = None  # "YYYY-MM-DD" MSK date — Sunday summary flag
_last_knowledge_expiry_date: Optional[str] = None  # "YYYY-MM-DD" UTC date — set when weekly expiry runs
_last_media_gc_hour: Optional[str] = None  # "YYYY-MM-DD HH" — set when hourly media GC runs
_last_dialogue_backfill_date: Optional[str] = None  # "YYYY-MM-DD" MSK — nightly dialogue mining flag
_ALLOWED_UPDATES = ["message", "callback_query"]


def _now_msk() -> datetime:
    return datetime.now(timezone.utc).astimezone(_MSK)


def _hm_matches(now_msk: datetime, target_h: int, target_m: int, window_min: int = _TRIGGER_WINDOW_MIN) -> bool:
    cur = now_msk.hour * 60 + now_msk.minute
    tgt = target_h * 60 + target_m
    return 0 <= (cur - tgt) < window_min

KNOWLEDGE_EXPIRY_DAYS = 180


def mark_report_done_today() -> None:
    """Mark today's report as done so the auto-run scheduler skips it.

    Sets the in-memory guard only. DB persistence is the caller's
    responsibility — they know which report_date to stamp.
    """
    global _last_report_date
    _last_report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


async def _maybe_reconcile_general(bot: Bot) -> None:
    """Run periodic HDE reconciliation during work hours to catch direct-HDE assignments."""
    global _last_general_reconcile_at
    if config.general_topic_id is None:
        return
    from .work_schedule import is_work_time
    if not is_work_time():
        return
    now = datetime.now(timezone.utc)
    if (
        _last_general_reconcile_at is not None
        and (now - _last_general_reconcile_at).total_seconds() < _GENERAL_RECONCILE_INTERVAL_SEC
    ):
        return
    _last_general_reconcile_at = now
    from .general_channel import reconcile_with_hde
    try:
        await reconcile_with_hde(bot)
    except Exception as exc:
        logger.warning("Periodic General reconcile failed: %s", exc)


async def _maybe_send_report_button(bot: Bot) -> None:
    """Mon–Thu 8:30 MSK: send an inline button prompting for yesterday's report."""
    global _report_button_sent
    from .reporting.runner import is_report_configured
    from .work_schedule import last_work_day
    if not is_report_configured():
        return
    now_msk = _now_msk()
    if not _hm_matches(now_msk, *_REPORT_MORNING_HM):
        return
    today = now_msk.strftime("%Y-%m-%d")
    if _report_button_sent == today or _last_report_date == today:
        return
    last_wd = last_work_day()
    if await db.is_report_sent(last_wd):
        logger.info("Report for %s already sent, skipping button", last_wd)
        _report_button_sent = today
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
        "Нажмите кнопку или подождите — через 30 минут отчёт сформируется автоматически.",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def _maybe_auto_run_report(bot: Bot) -> None:
    """Mon–Thu 9:00 MSK: auto-run the previous work day's report if not sent."""
    global _last_report_date
    from .reporting.runner import is_report_configured, run_report
    from .work_schedule import last_work_day
    if not is_report_configured():
        return
    now_msk = _now_msk()
    if not _hm_matches(now_msk, *_REPORT_AUTORUN_HM):
        return
    today = now_msk.strftime("%Y-%m-%d")
    if _last_report_date == today:
        return
    last_wd = last_work_day()
    # Claim the date BEFORE running: the slow scrape+append happens inside
    # run_report, so marking after it leaves a window where a second trigger
    # (another instance / restart) also sees "not sent" and writes a duplicate.
    if not await db.mark_report_sent(last_wd):
        _last_report_date = today
        return
    _last_report_date = today
    try:
        result = await run_report(last_wd)
    except Exception as exc:
        await db.clear_report_sent(last_wd)  # nothing written — allow a retry
        logger.exception("Daily report failed: %s", exc)
        await bot.send_message(
            config.personal_chat_id,
            f"❌ <b>Ошибка при формировании отчёта:</b>\n<code>{exc}</code>",
            parse_mode="HTML",
        )
        return
    await bot.send_message(config.personal_chat_id, result, parse_mode="HTML")


async def _maybe_thursday_evening_autorun(bot: Bot) -> None:
    """Thu 19:00 MSK: auto-fill today's (Thursday) report without a prompt.

    Fri/Sat are days off — this avoids waiting until Sunday to close Thursday.
    """
    global _thursday_evening_done, _last_report_date
    from .reporting.runner import is_report_configured, run_report
    if not is_report_configured():
        return
    now_msk = _now_msk()
    if not _hm_matches(now_msk, *_THU_EVENING_HM):
        return
    today_date = now_msk.date()
    today_key = today_date.strftime("%Y-%m-%d")
    if _thursday_evening_done == today_key:
        return
    # Claim the date BEFORE running (see _maybe_auto_run_report): prevents a
    # second trigger in the 19:00 window from appending a duplicate row.
    if not await db.mark_report_sent(today_date):
        _thursday_evening_done = today_key
        return
    _thursday_evening_done = today_key
    _last_report_date = today_key
    try:
        result = await run_report(today_date)
    except Exception as exc:
        await db.clear_report_sent(today_date)  # nothing written — allow a retry
        logger.exception("Thursday evening report failed: %s", exc)
        await bot.send_message(
            config.personal_chat_id,
            f"❌ <b>Ошибка при формировании отчёта:</b>\n<code>{exc}</code>",
            parse_mode="HTML",
        )
        return
    await bot.send_message(config.personal_chat_id, result, parse_mode="HTML")


async def _maybe_weekly_summary(bot: Bot) -> None:
    """Sun 8:30 MSK: send a summary of the previous work week (Sun–Thu)."""
    global _weekly_summary_done
    from .reporting.runner import is_report_configured, run_weekly_summary
    if not is_report_configured():
        return
    now_msk = _now_msk()
    if not _hm_matches(now_msk, *_WEEKLY_SUMMARY_HM):
        return
    today_date = now_msk.date()
    today_key = today_date.strftime("%Y-%m-%d")
    if _weekly_summary_done == today_key:
        return
    _weekly_summary_done = today_key
    try:
        result = await run_weekly_summary(today_date)
        await bot.send_message(config.personal_chat_id, result, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Weekly summary failed: %s", exc)
        await bot.send_message(
            config.personal_chat_id,
            f"❌ <b>Ошибка при формировании недельной сводки:</b>\n<code>{exc}</code>",
            parse_mode="HTML",
        )



def _resolved_before_cutoff(ticket: dict, now: datetime, days: int = 7) -> bool:
    """True если тикет закрыт ≥*days* дней назад (свежие могут переоткрыться).

    Дата закрытия/обновления берётся из первого доступного ключа raw-тикета.
    Если поля нет или оно не парсится — возвращаем True (майним), т.к. точный
    timestamp резолюции не гарантирован в списочном ответе (см. Task 0 note)."""
    cutoff = now - timedelta(days=days)
    for key in ("resolved_at", "date_resolved", "date_closed", "date_updated", "updated_at"):
        raw = ticket.get(key)
        if not raw:
            continue
        try:
            dt = parse_datetime(str(raw))
        except Exception:
            dt = None
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= cutoff
    return True


async def _maybe_backfill_dialogue_pairs(bot) -> None:
    """Ночной инкремент dialogue_pairs (Phase 2A). Отдельный флаг
    agent_dialogue_mining_enabled — НЕ зависит от agent_enabled. Берёт только
    тикеты, закрытые >7 дней назад (свежие могут переоткрыться)."""
    global _last_dialogue_backfill_date
    if not config.agent_dialogue_mining_enabled:
        return
    now = _now_msk()
    today = now.strftime("%Y-%m-%d")
    if _last_dialogue_backfill_date == today or now.hour != 1:
        return
    _last_dialogue_backfill_date = today
    try:
        from .agent.dialogue_mining import mine_ticket_pairs, staff_id_set
        from .hde_api import HDEApiClient
        client = HDEApiClient()
        staff = staff_id_set(config.hde_owner_id, config.agent_staff_user_ids)
        known = await db.dialogue_pair_hashes()
        processed = await db.list_processed_ticket_ids()
        tickets, _ = await client.get_closed_tickets_page(config.hde_owner_id, 1)
        saved = 0
        for ticket in tickets:
            tid = str(ticket.get("id") or ticket.get("ticket_id") or "")
            if not tid or tid in processed:
                continue
            if not _resolved_before_cutoff(ticket, now):  # 7-day rule
                continue
            try:
                saved += await mine_ticket_pairs(client, ticket, staff, known_hashes=known)
                await db.mark_ticket_processed(tid)
            except Exception as exc:
                await db.log_ticket_error(tid, str(exc))
        logger.info("Nightly dialogue backfill: +%d pairs", saved)
    except Exception as exc:
        logger.warning("Nightly dialogue backfill failed: %s", exc)


async def process_scheduled_actions(bot: Bot) -> None:
    from .work_schedule import is_work_day, is_work_time, last_work_day, was_yesterday_work_day

    # Digest + overnight General flush fire on any work day at digest hour.
    # Flush runs FIRST so the digest's unassigned-equipment count reflects the
    # post-reconcile state (matches the personal summary).
    if is_work_day():
        await _maybe_flush_general(bot)
        await _maybe_send_digest(bot)

    # Periodic HDE↔General reconciliation during work hours: catches direct-HDE
    # assignments when HDE doesn't fire a usable webhook (or sends one with
    # empty owner_name). Cheap: 1 list call + delta-only edits every ~7 min.
    await _maybe_reconcile_general(bot)

    # Report schedule:
    #   Mon–Thu 8:30 MSK  → reminder button for previous work day
    #   Mon–Thu 9:00 MSK  → auto-run if button not pressed within 30 min
    #   Thu      19:00 MSK → auto-fill today's report (skip if already sent)
    #   Sun       8:30 MSK → weekly summary for previous work week
    # is_work_day() already returns False during vacation.
    if is_work_day():
        weekday_msk = _now_msk().weekday()
        if weekday_msk in (0, 1, 2, 3):  # Mon–Thu
            await _maybe_send_report_button(bot)
            await _maybe_auto_run_report(bot)
        if weekday_msk == 3:             # Thu
            await _maybe_thursday_evening_autorun(bot)
        if weekday_msk == 6:             # Sun
            await _maybe_weekly_summary(bot)

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

    # Nightly dialogue_pairs increment (Phase 2A) — own flag, 7-day cutoff.
    await _maybe_backfill_dialogue_pairs(bot)

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

    # Nightly prompt optimizer disabled: the hand-tuned _FORMAT_INSTRUCTIONS is
    # the source of truth. Manual runs still available via /aioptimize.

    # Pre-SLA and pending deletions require both work day AND work hours
    if not is_work_time():
        return

    now_value = to_storage(utcnow())

    # One HDE check per (ticket, client-reply) per pass: the three loops below
    # may ask about the same ticket. Cache lives only within this pass.
    verify_cache: dict[tuple[str, Optional[str]], bool] = {}

    async def _staff_replied(record) -> bool:
        key = (record.ticket_id, record.last_client_reply_at)
        if key not in verify_cache:
            verify_cache[key] = await _hde_staff_replied_since(
                record.ticket_id, record.last_client_reply_at
            )
        return verify_cache[key]

    for record in await db.list_due_pre_sla(now_value):
        if await _staff_replied(record):
            logger.info(
                "pre-SLA skipped for ticket %s: operator already replied in HDE (self-heal)",
                record.ticket_id,
            )
            await db.clear_pre_sla(record.ticket_id)
            continue
        try:
            await send_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error("Failed to send pre-SLA alert for ticket %s: %s", record.ticket_id, exc)

    active_pre_sla = await db.list_active_pre_sla()

    for record in active_pre_sla:
        if record.pre_sla_sent_at:
            last_update = parse_datetime(record.pre_sla_sent_at)
            if last_update and (utcnow() - last_update).total_seconds() < 55:
                continue
        if await _staff_replied(record):
            logger.info(
                "pre-SLA countdown cleared for ticket %s: operator replied in HDE",
                record.ticket_id,
            )
            try:
                await _try_delete_pre_sla_message(bot, record)
            except Exception as exc:
                logger.debug("pre-SLA message cleanup failed for %s: %s", record.ticket_id, exc)
            await db.clear_pre_sla(record.ticket_id)
            continue
        try:
            await update_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error(
                "Failed to update pre-SLA countdown for ticket %s: %s",
                record.ticket_id, exc,
            )

    for record in active_pre_sla:
        if record.reassurance_sent_at is not None:
            continue
        deadline = parse_datetime(record.pre_sla_notify_at)
        if deadline is None:
            continue
        from datetime import timedelta as _td
        sla_deadline = deadline + _td(minutes=config.pre_sla_warning_minutes)
        minutes_left = (sla_deadline - utcnow()).total_seconds() / 60
        if minutes_left > config.reassurance_minutes_before:
            continue
        if await _staff_replied(record):
            await db.clear_pre_sla(record.ticket_id)
            continue
        try:
            await send_reassurance_to_client(bot, record)
        except Exception as exc:
            logger.warning("Reassurance failed for ticket %s: %s", record.ticket_id, exc)

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
