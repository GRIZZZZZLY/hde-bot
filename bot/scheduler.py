from __future__ import annotations

import asyncio
import logging
import os
import time
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import db, operators
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
_last_value_report_date: Optional[str] = None   # daily AI-value report
_last_general_flush_date: Optional[str] = None  # "YYYY-MM-DD" UTC date
_last_general_reconcile_at: Optional[datetime] = None  # last in-hours reconcile time
_GENERAL_RECONCILE_INTERVAL_SEC = 7 * 60
_last_db_backup_date: Optional[str] = None  # "YYYY-MM-DD" UTC — суточный снапшот базы
_last_report_date: Optional[str] = None         # "YYYY-MM-DD" UTC date — set when report runs
_report_button_sent: Optional[str] = None  # "YYYY-MM-DD" UTC date — set when button is sent
_thursday_evening_done: Optional[str] = None  # "YYYY-MM-DD" MSK date — Thu evening auto-run flag
_weekly_summary_done: Optional[str] = None  # "YYYY-MM-DD" MSK date — Sunday summary flag
_last_knowledge_expiry_date: Optional[str] = None  # "YYYY-MM-DD" UTC date — set when weekly expiry runs
_last_media_gc_hour: Optional[str] = None  # "YYYY-MM-DD HH" — set when hourly media GC runs
_last_dialogue_backfill_date: Optional[str] = None  # "YYYY-MM-DD" MSK — nightly dialogue mining flag
_last_reconcile_date: Optional[str] = None  # "YYYY-MM-DD" MSK — nightly answer reconciliation flag
_last_reconcile_digest_date: Optional[str] = None  # "YYYY-MM-DD" UTC — reconciliation digest flag
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
    if not config.morning_digest_enabled:
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour != config.digest_send_hour_utc:
        return
    if _last_digest_date == today:
        return
    _last_digest_date = today
    from .digest import send_morning_digest
    await send_morning_digest(bot)


async def _maybe_send_daily_value_report(bot: Bot) -> None:
    """Ежедневный отчёт пользы AI-подсказок (ревизия 3 roadmap) — тем же утром,
    что и дайджест. Пустой день (0 подсказок) — не отправляется."""
    global _last_value_report_date
    if not config.personal_digests_enabled:
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour != config.digest_send_hour_utc:
        return
    if _last_value_report_date == today:
        return
    _last_value_report_date = today
    from .agent.value_report import send_daily_value_report
    try:
        await send_daily_value_report(bot)
    except Exception as exc:
        logger.warning("Daily value report failed: %s", exc)


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
    from .work_schedule import anyone_at_work
    if not anyone_at_work():
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
    from .work_schedule import is_vacation_day
    if await is_vacation_day(last_wd):
        logger.info("%s was a vacation day, skipping report button", last_wd)
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
    # A vacation day has no closed tickets, and HDE omits zero-activity
    # operators from the staff report — scraping it fails instead of returning 0.
    from .work_schedule import is_vacation_day
    if await is_vacation_day(last_wd):
        logger.info("%s was a vacation day, skipping the report", last_wd)
        await db.mark_report_sent(last_wd)  # nothing to fill — stop nagging
        _last_report_date = today
        await bot.send_message(
            config.personal_chat_id,
            f"🏖 <b>{last_wd.strftime('%d.%m.%Y')} — отпуск</b>, отчёт пропущен.",
            parse_mode="HTML",
        )
        return
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


async def run_dialogue_backfill(
    *, max_pages: int = 10, _client=None, _now=None
) -> dict:
    """Пагинированный майнинг закрытых тикетов (закрыт >7 дней назад).

    Страница 1 — самые свежие закрытые, они моложе отсечки; майнимое окно лежит
    глубже, поэтому идём по страницам до max_pages. Стоп раньше — на странице,
    где нет ни свежих, ни новых тикетов (история уже обработана)."""
    from .agent.dialogue_mining import mine_ticket_pairs, staff_id_set
    from .hde_api import HDEApiClient
    client = _client or HDEApiClient()
    now = _now or _now_msk()
    staff = staff_id_set(config.hde_owner_id, config.agent_staff_user_ids)
    staff_cache: dict = {}
    known = await db.dialogue_pair_hashes()
    processed = await db.list_processed_ticket_ids()
    stats = {"pages": 0, "new_pairs": 0, "mined_tickets": 0,
             "fresh": 0, "known": 0, "errors": 0}
    for page in range(1, max_pages + 1):
        tickets, total_pages = await client.get_closed_tickets_page(
            config.hde_owner_id, page
        )
        if not tickets:
            break
        stats["pages"] += 1
        page_fresh = page_new = 0
        for ticket in tickets:
            tid = str(ticket.get("id") or ticket.get("ticket_id") or "")
            if not tid or tid in processed:
                stats["known"] += 1
                continue
            if not _resolved_before_cutoff(ticket, now):  # 7-day rule
                stats["fresh"] += 1
                page_fresh += 1
                continue
            page_new += 1
            try:
                stats["new_pairs"] += await mine_ticket_pairs(
                    client, ticket, staff,
                    known_hashes=known, staff_cache=staff_cache,
                )
                stats["mined_tickets"] += 1
                await db.mark_ticket_processed(tid)
            except Exception as exc:
                stats["errors"] += 1
                await db.log_ticket_error(tid, str(exc))
        if page_fresh == 0 and page_new == 0:  # зона обработанной истории
            break
        if page >= total_pages:
            break
    return stats


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
        started = time.monotonic()
        stats = await run_dialogue_backfill()
        logger.info("Nightly dialogue backfill: %s", stats)
        from .agent.pair_quality import gate_pending_pairs
        # Дедлайн — остаток бюджета джоба за вычетом времени бэкфилла.
        gate_stats = await gate_pending_pairs(
            limit=300,
            deadline_s=max(_PAIR_GATE_DEADLINE_SECONDS - (time.monotonic() - started), 0),
        )
        logger.info("Nightly pair gating: %s", gate_stats)
    except Exception as exc:
        logger.warning("Nightly dialogue backfill failed: %s", exc)


async def _maybe_send_reconciliation_digest(bot: Bot) -> None:
    """Утренняя сводка ночной сверки в личный чат (раз в сутки, в час дайджеста)."""
    global _last_reconcile_digest_date
    if not config.personal_digests_enabled:
        return
    if not config.agent_dialogue_mining_enabled:
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour != config.digest_send_hour_utc or _last_reconcile_digest_date == today:
        return
    _last_reconcile_digest_date = today
    from .digest import send_reconciliation_digest
    await send_reconciliation_digest(bot)


async def _maybe_reconcile_answers(bot) -> None:
    """Ночная сверка (02:00 MSK) предложений бота с фактическими ответами
    операторов. Раз в сутки; вердикты ложатся в judge-поля ai_suggestions,
    утренняя сводка их показывает. Замер идёт под nightly_reconcile_enabled
    (тихий режим) и agent_dialogue_mining_enabled; пополнение базы знаний
    (разбор очереди кандидатов + архивация авто-правил) — отдельным флагом
    reconcile_kb_distill_enabled."""
    global _last_reconcile_date
    if not config.nightly_reconcile_enabled:
        return
    if not config.agent_dialogue_mining_enabled:
        return
    now = _now_msk()
    today = now.strftime("%Y-%m-%d")
    if _last_reconcile_date == today or now.hour != 2:
        return
    _last_reconcile_date = today
    try:
        from .agent.reconcile import reconcile_recent
        stats = await reconcile_recent(hours=24)
        logger.info("Nightly answer reconciliation: %s", stats)
    except Exception as exc:
        logger.warning("Nightly answer reconciliation failed: %s", exc)
    if not config.reconcile_kb_distill_enabled:
        return
    # Разбор очереди кандидатов идёт СРАЗУ после сверки, тем же проходом: к
    # утренней сводке очередь должна быть уже разобрана, иначе владелец опять
    # получит десяток решений по фактам, а не одну строку с итогами.
    try:
        from .agent.kb_distill import process_pending_candidates
        kb_stats = await process_pending_candidates()
        if any(kb_stats.values()):
            logger.info("Nightly KB candidate distillation: %s", kb_stats)
    except Exception as exc:
        logger.warning("Nightly KB candidate distillation failed: %s", exc)
    try:
        archived = await db.archive_unused_auto_rules(days=60)
        if archived:
            logger.info("Archived %d unused auto-rules", archived)
    except Exception as exc:
        logger.warning("Auto-rule archival failed: %s", exc)


async def _maybe_refresh_stale_drafts(bot: Bot) -> None:
    """Пересборка черновиков, устаревших из-за комментария коллеги.

    Только в рабочее время: черновик нужен оператору сейчас, а не ночью, и
    ночные пачки уже занимают квоту Groq. Свой флаг — расход токенов заметен на
    free-tier (см. bot/agent/draft_refresh.py)."""
    if not config.agent_draft_refresh_enabled:
        return
    from .work_schedule import is_work_time
    if not is_work_time():
        return
    from .agent.draft_refresh import refresh_stale_drafts
    try:
        stats = await refresh_stale_drafts(bot)
        if stats.get("refreshed") or stats.get("errors"):
            logger.info("Draft refresh: %s", stats)
    except Exception as exc:
        logger.warning("Draft refresh pass failed: %s", exc)


async def _maybe_backup_db(bot: Bot) -> None:
    """Суточный снапшот базы (VACUUM INTO). Раз в сутки, в тихий час UTC.

    Провал докладывается оператору: молчаливо не сделанный бэкап хуже
    отсутствующего — о нём узнают в момент, когда он понадобился. Дата
    отмечается ДО попытки, чтобы неудача не повторялась каждые 30 секунд.
    """
    global _last_db_backup_date
    if not config.db_backup_dir:
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour != config.db_backup_hour_utc or _last_db_backup_date == today:
        return
    _last_db_backup_date = today
    from .db.backup import backup_database
    try:
        await backup_database()
    except Exception as exc:
        logger.error("DB backup failed: %s", exc)
        try:
            await bot.send_message(
                config.personal_chat_id,
                f"⚠️ Бэкап базы не сделан: {exc}",
            )
        except Exception as send_exc:
            logger.warning("DB backup alert not delivered: %s", send_exc)


_JOB_TIMEOUT_SECONDS = 90
_TIMERS_TIMEOUT_SECONDS = 120
_REPORT_TIMEOUT_SECONDS = 600

# Ночная пачка dialogue_pairs (01:xx MSK) идёт по HDE API с пейсингом, а потом
# гейтит пары по одному LLM-вызову с паузой 6 с под TPM — в 600 с она не влезает
# никогда и каждую ночь падала по таймауту. Свой лимит: пачка идёт в час, когда
# нечего рассылать (рабочее окно 9–18), поэтому серийность прохода не мешает.
# Гейт получает дедлайн НИЖЕ этого таймаута, чтобы выйти самому и записать stats.
_NIGHTLY_BATCH_TIMEOUT_SECONDS = 1500
_PAIR_GATE_DEADLINE_SECONDS = 1200

# Ссылки на фоновые таски держим до завершения: без этого сборщик мусора может
# забрать таск на полпути.
_background_tasks: set[asyncio.Task] = set()
_report_task: Optional[asyncio.Task] = None


async def _run_job(name: str, coro, *, timeout: float | None = None) -> bool:
    """Джоб под таймаутом. Ни исключение, ни таймаут не роняют проход.

    Без таймаута один зависший await (HDE API без ответа, Playwright, Telegram)
    останавливает ВЕСЬ проход: pre-SLA-будильники стоят в той же очереди и
    молчат. Watchdog от этого не спасает — он отдельный таск и продолжает
    пинговать systemd, то есть процесс выглядит живым, пока таймеры не работают.

    timeout=None → значение модуля читается ПРИ ВЫЗОВЕ: как дефолт аргумента оно
    зафиксировалось бы при импорте и перестало настраиваться.
    """
    if timeout is None:
        timeout = _JOB_TIMEOUT_SECONDS
    started = time.monotonic()
    try:
        await asyncio.wait_for(coro, timeout=timeout)
        return True
    except asyncio.TimeoutError:
        logger.error("Scheduler job %s timed out after %.0fs", name, timeout)
    except Exception as exc:
        logger.warning("Scheduler job %s failed: %s", name, exc)
    finally:
        elapsed = time.monotonic() - started
        if elapsed > timeout / 2:
            logger.info("Scheduler job %s took %.1fs", name, elapsed)
    return False


def _spawn_report(name: str, coro) -> bool:
    """Отчёт уходит в ФОН и не задерживает проход.

    Playwright открывает браузер и ходит по UI HDE — это минуты, а в том же
    проходе стоят pre-SLA-будильники. Single-flight обязателен: два браузера на
    один профиль конкурируют и падают оба, поэтому повторный запуск, пока
    предыдущий жив, отбрасывается.
    """
    global _report_task
    if _report_task is not None and not _report_task.done():
        logger.info("Report job %s skipped: previous run still active", name)
        coro.close()
        return False
    _report_task = asyncio.create_task(
        _run_job(name, coro, timeout=_REPORT_TIMEOUT_SECONDS)
    )
    _background_tasks.add(_report_task)
    _report_task.add_done_callback(_background_tasks.discard)
    return True


_INBOX_DEAD_ALERT_KEY = "inbox_dead_alerted"


async def _maybe_alert_dead_inbox(bot: Bot) -> None:
    """Алерт по событиям инбокса, застрявшим в 'dead'.

    Строка в 'dead' — это webhook, который не обработался за все попытки: клиент
    написал, а топик не появился, и SLA тикает молча. Это единственный сбой
    инбокса, который не виден вообще никак, поэтому о нём сообщаем.

    Алерт по РОСТУ счётчика, а не по факту непустого: иначе он повторялся бы
    каждые 30 секунд. Число уже разосланных живёт в bot_settings, а не в памяти:
    строки в 'dead' переживают рестарт, и in-memory guard кричал бы про старые
    события при каждом подъёме процесса.
    """
    counts = await db.count_inbox_by_status()
    dead = int(counts.get("dead", 0) or 0)
    alerted = int(await db.get_setting(_INBOX_DEAD_ALERT_KEY, "0") or 0)
    if dead < alerted:
        # Очередь почистили — опускаем планку, иначе следующий сбой промолчит.
        await db.set_setting(_INBOX_DEAD_ALERT_KEY, str(dead))
        return
    if dead <= alerted:
        return
    await db.set_setting(_INBOX_DEAD_ALERT_KEY, str(dead))
    logger.error("Inbox: %d event(s) in 'dead' — webhooks were never processed", dead)
    try:
        await bot.send_message(
            config.personal_chat_id,
            f"🚨 Инбокс: {dead} событий в статусе dead — вебхуки не обработаны "
            f"после всех попыток.\nТикеты могли не появиться в Telegram, "
            f"SLA по ним идёт незаметно. Разбивка — в /status.",
        )
    except Exception as exc:
        logger.warning("Inbox dead alert not delivered: %s", exc)


async def process_scheduled_actions(bot: Bot) -> None:
    from .work_schedule import is_work_day, is_work_time, last_work_day, was_yesterday_work_day

    # Digest + overnight General flush fire on any work day at digest hour.
    # Flush runs FIRST so the digest's unassigned-equipment count reflects the
    # post-reconcile state (matches the personal summary).
    if is_work_day():
        await _run_job("flush_general", _maybe_flush_general(bot))
        await _run_job("digest", _maybe_send_digest(bot))
        await _run_job("value_report", _maybe_send_daily_value_report(bot))
        await _run_job("reconcile_digest", _maybe_send_reconciliation_digest(bot))

    # Periodic HDE↔General reconciliation during work hours: catches direct-HDE
    # assignments when HDE doesn't fire a usable webhook (or sends one with
    # empty owner_name). Cheap: 1 list call + delta-only edits every ~7 min.
    await _run_job("reconcile_general", _maybe_reconcile_general(bot))

    # Report schedule:
    #   Mon–Thu 8:30 MSK  → reminder button for previous work day
    #   Mon–Thu 9:00 MSK  → auto-run if button not pressed within 30 min
    #   Thu      19:00 MSK → auto-fill today's report (skip if already sent)
    #   Sun       8:30 MSK → weekly summary for previous work week
    # is_work_day() already returns False during vacation.
    # Окно проверяется ЗДЕСЬ, а не только внутри джоба: _spawn_report держит
    # один слот на все отчёты, и джоб, спавнящийся каждый проход, занимает его
    # ещё до собственной проверки времени. Свежесозданный таск не done(), так
    # что следующий _spawn_report в том же проходе отбрасывался всегда.
    if is_work_day():
        now_msk = _now_msk()
        weekday_msk = now_msk.weekday()
        if weekday_msk in (0, 1, 2, 3):  # Mon–Thu
            await _run_job("report_button", _maybe_send_report_button(bot))
            # Прогоны отчёта — в фон: Playwright занимает минуты (см. _spawn_report)
            if _hm_matches(now_msk, *_REPORT_AUTORUN_HM):
                _spawn_report("report_autorun", _maybe_auto_run_report(bot))
        if weekday_msk == 3 and _hm_matches(now_msk, *_THU_EVENING_HM):
            _spawn_report("report_thursday", _maybe_thursday_evening_autorun(bot))
        if weekday_msk == 6 and _hm_matches(now_msk, *_WEEKLY_SUMMARY_HM):
            _spawn_report("weekly_summary", _maybe_weekly_summary(bot))

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
    # Ночные пачки ходят по HDE API с пейсингом и живут дольше обычного джоба.
    await _run_job(
        "dialogue_backfill", _maybe_backfill_dialogue_pairs(bot),
        timeout=_NIGHTLY_BATCH_TIMEOUT_SECONDS,
    )

    # Nightly answer reconciliation: сверка предложений бота с фактическими
    # ответами операторов (learning без кнопок). Результат — в judge-поля.
    await _run_job(
        "reconcile_answers", _maybe_reconcile_answers(bot),
        timeout=_REPORT_TIMEOUT_SECONDS,
    )

    # Пересборка черновиков, устаревших из-за комментария коллеги. Дневной джоб:
    # обновлённый черновик нужен оператору в смену, а не в 02:00.
    await _run_job(
        "draft_refresh", _maybe_refresh_stale_drafts(bot),
        timeout=_REPORT_TIMEOUT_SECONDS,
    )

    # Суточный снапшот базы: схема не версионируется, откат релиза данные не
    # откатывает. VACUUM INTO на 240 МБ — десятки секунд.
    await _run_job("db_backup", _maybe_backup_db(bot), timeout=_REPORT_TIMEOUT_SECONDS)

    # Инбокс: события, застрявшие в 'dead', — не доехавшие тикеты.
    await _run_job("inbox_dead_alert", _maybe_alert_dead_inbox(bot), timeout=15)

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

    # Pre-SLA and pending deletions require both work day AND work hours.
    # Вынесены в отдельный джоб под таймаутом: это самая чувствительная к
    # задержке часть прохода, и повиснуть она не должна ни на HDE-сверке, ни на
    # Telegram.
    from .work_schedule import anyone_at_work
    if not anyone_at_work():
        return
    await _run_job(
        "due_timers", _process_due_timers(bot), timeout=_TIMERS_TIMEOUT_SECONDS
    )


async def _process_due_timers(bot: Bot) -> None:
    """Pre-SLA будильники, обратный отсчёт, reassurance и отложенные удаления."""
    now_value = to_storage(utcnow())

    # One HDE check per (ticket, client-reply) per pass: the three loops below
    # may ask about the same ticket. Cache lives only within this pass.
    verify_cache: dict[tuple[str, Optional[str]], bool] = {}

    from .work_schedule import is_work_time_for

    def _at_work(record) -> bool:
        return is_work_time_for(operators.by_chat(record.chat_id))

    async def _staff_replied(record) -> bool:
        key = (record.ticket_id, record.last_client_reply_at)
        if key not in verify_cache:
            verify_cache[key] = await _hde_staff_replied_since(
                record.ticket_id, record.last_client_reply_at
            )
        return verify_cache[key]

    for record in await db.list_due_pre_sla(now_value):
        if not _at_work(record):
            continue
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
        if not _at_work(record):
            continue
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
        owner = operators.by_chat(record.chat_id)
        if owner is None or not owner.auto_reassurance or not _at_work(record):
            continue  # nobody agreed to automatic messages to this engineer's clients
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
