"""Report commands: /report and /weekly."""
import logging
from datetime import date

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()


async def run_daily_report(target: Message, report_date: date | None) -> None:
    """Daily report for `report_date`, or for the last work day when None."""
    from ..reporting import runner
    from ..config import config
    from ..work_schedule import last_work_day

    if not runner.is_report_configured():
        await target.answer(
            "⚠️ Отчёт не настроен.\n\n"
            "Добавьте в .env:\n"
            "<code>HDE_REPORT_PASSWORD=...\n"
            "GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google_service_account.json\n"
            "GOOGLE_SPREADSHEET_ID=...</code>",
            parse_mode="HTML",
        )
        return

    day = report_date or last_work_day()
    if day.weekday() not in config.work_days:
        await target.answer(
            f"🏖 <b>{day.strftime('%d.%m.%Y')} был выходной</b> — данных для отчёта нет.",
            parse_mode="HTML",
        )
        return

    wait_msg = await target.answer("⏳ Отчёт генерируется, подождите...")
    try:
        result = await runner.run_report(day)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("report: wait message cleanup failed: %s", exc)
    await target.answer(result, parse_mode="HTML")


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject) -> None:
    """
    /report          — отчёт за прошлый рабочий день
    /report 2025-04-04  — отчёт за конкретную дату (YYYY-MM-DD)
    """
    report_date: date | None = None
    if command.args:
        try:
            report_date = date.fromisoformat(command.args.strip())
        except ValueError:
            await message.answer(
                "⚠️ Неверный формат даты. Используйте: <code>/report YYYY-MM-DD</code>",
                parse_mode="HTML",
            )
            return
    await run_daily_report(message, report_date)


async def run_weekly(target: Message, today: date | None) -> None:
    """Weekly summary as of Sunday `today` (None = real today)."""
    from ..reporting import runner

    if not runner.is_report_configured():
        await target.answer(
            "⚠️ Отчёт не настроен.\n\n"
            "Добавьте в .env:\n"
            "<code>GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google_service_account.json\n"
            "GOOGLE_SPREADSHEET_ID=...</code>",
            parse_mode="HTML",
        )
        return

    wait_msg = await target.answer("⏳ Считаю недельную сводку...")
    try:
        result = await runner.run_weekly_summary(today)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("weekly: wait message cleanup failed: %s", exc)
    await target.answer(result, parse_mode="HTML")


@router.message(Command("weekly"))
async def cmd_weekly(message: Message, command: CommandObject) -> None:
    """
    /weekly            — сводка за прошлую рабочую неделю
    /weekly 2026-09-06 — сводка на конкретное воскресенье (YYYY-MM-DD)

    Нужна, когда воскресная авторассылка упала: планировщик помечает день
    отправленным до попытки и сам не повторяет.
    """
    today: date | None = None
    if command.args:
        try:
            today = date.fromisoformat(command.args.strip())
        except ValueError:
            await message.answer(
                "⚠️ Неверный формат даты. Используйте: <code>/weekly YYYY-MM-DD</code>",
                parse_mode="HTML",
            )
            return
    await run_weekly(message, today)
