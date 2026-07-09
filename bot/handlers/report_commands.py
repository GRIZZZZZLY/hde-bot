"""Report commands: /report and report:* callbacks."""
import logging
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ..db import mark_report_sent

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject) -> None:
    """
    /report          — отчёт за вчера
    /report 2025-04-04  — отчёт за конкретную дату (YYYY-MM-DD)
    """
    from ..reporting.runner import is_report_configured, run_report

    if not is_report_configured():
        await message.answer(
            "⚠️ Отчёт не настроен.\n\n"
            "Добавьте в .env:\n"
            "<code>HDE_REPORT_PASSWORD=...\n"
            "GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google_service_account.json\n"
            "GOOGLE_SPREADSHEET_ID=...</code>",
            parse_mode="HTML",
        )
        return

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

    # Check if target date was a work day
    from ..config import config
    from ..work_schedule import last_work_day
    target = report_date if report_date else last_work_day()
    target_weekday = target.weekday()
    if target_weekday not in config.work_days:
        await message.answer(
            f"🏖 <b>{target.strftime('%d.%m.%Y')} был выходной</b> — данных для отчёта нет.",
            parse_mode="HTML",
        )
        return

    wait_msg = await message.answer("⏳ Отчёт генерируется, подождите...")
    try:
        result = await run_report(report_date)
        from ..scheduler import mark_report_done_today
        mark_report_done_today()
        await mark_report_sent(target)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("report: wait message cleanup failed: %s", exc)
    await message.answer(result, parse_mode="HTML")


@router.callback_query(F.data == "report:cancel")
async def cb_report_cancel(callback: CallbackQuery) -> None:
    """Inline button: dismiss the report reminder without running the report."""
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("report: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data == "report:run_yesterday")
async def cb_report_yesterday(callback: CallbackQuery) -> None:
    """Inline button: run yesterday's report from the personal chat prompt."""
    from ..reporting.runner import run_report
    from ..scheduler import mark_report_done_today

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("report: reply markup cleanup failed: %s", exc)

    from ..work_schedule import was_yesterday_work_day
    if not was_yesterday_work_day():
        yesterday = date.today() - timedelta(days=1)
        await callback.message.answer(
            f"🏖 <b>Вчера ({yesterday.strftime('%d.%m.%Y')}) был выходной</b> — данных для отчёта нет.",
            parse_mode="HTML",
        )
        return

    wait_msg = await callback.message.answer("⏳ Отчёт генерируется, подождите...")
    try:
        result = await run_report()
        mark_report_done_today()
        yesterday = date.today() - timedelta(days=1)
        await mark_report_sent(yesterday)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("report: wait message cleanup failed: %s", exc)
    await callback.message.answer(result, parse_mode="HTML")
