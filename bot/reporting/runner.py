"""
Daily HDE -> Google Sheets report pipeline.

Called from:
- bot/scheduler.py
- bot/handlers/commands.py

Required env vars:
- HDE_API_BASE_URL
- HDE_API_EMAIL
- HDE_OWNER_NAME
- HDE_REPORT_PASSWORD or HDE_API_KEY
- GOOGLE_SERVICE_ACCOUNT_FILE
- GOOGLE_SPREADSHEET_ID

Backward compatibility:
- GOOGLE_CREDENTIALS_FILE is still accepted as a legacy alias for
  GOOGLE_SERVICE_ACCOUNT_FILE.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from .google_sheets import append_operator_row, read_weekly_tickets
from .hde_playwright import get_operator_report_data

load_dotenv()

logger = logging.getLogger(__name__)


def _get(name: str, fallback: str | None = None) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    if fallback is not None and fallback.strip():
        return fallback.strip()
    raise RuntimeError(
        f"Env var {name!r} is not set. Add it to .env to enable the daily report."
    )


def _get_google_service_account_file(required: bool = True) -> str:
    path = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        or os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip()
    )
    if not path:
        if required:
            raise RuntimeError(
                "Google service account file is not configured. "
                "Set GOOGLE_SERVICE_ACCOUNT_FILE in .env."
            )
        return ""
    return path


def is_report_configured() -> bool:
    """Return True only when the daily report can run unattended."""
    password_ok = bool(
        os.getenv("HDE_REPORT_PASSWORD", "").strip()
        or os.getenv("HDE_API_KEY", "").strip()
    )
    google_key = _get_google_service_account_file(required=False)

    checks = {
        "HDE_API_BASE_URL": bool(os.getenv("HDE_API_BASE_URL", "").strip()),
        "HDE_API_EMAIL": bool(os.getenv("HDE_API_EMAIL", "").strip()),
        "HDE_OWNER_NAME": bool(os.getenv("HDE_OWNER_NAME", "").strip()),
        "HDE_REPORT_PASSWORD/HDE_API_KEY": password_ok,
        "GOOGLE_SPREADSHEET_ID": bool(os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()),
        "GOOGLE_SERVICE_ACCOUNT_FILE (set)": bool(google_key),
        "GOOGLE_SERVICE_ACCOUNT_FILE (exists)": bool(google_key) and Path(google_key).exists(),
    }
    missing = [k for k, v in checks.items() if not v]
    if missing:
        logger.warning("Report not configured, missing: %s", ", ".join(missing))
        return False
    return True


async def run_report(report_date: date | None = None) -> str:
    """
    Run the full pipeline for *report_date* (defaults to yesterday).

    Returns a formatted status string for Telegram.
    Raises RuntimeError with a descriptive message on failure.
    """
    if report_date is None:
        report_date = date.today() - timedelta(days=1)

    hde_base_url = os.getenv("HDE_API_BASE_URL", "").replace("/api/v2", "").rstrip("/")
    if not hde_base_url:
        raise RuntimeError("HDE_API_BASE_URL is not configured")

    hde_login = _get("HDE_API_EMAIL")
    hde_password = _get("HDE_REPORT_PASSWORD", os.getenv("HDE_API_KEY", ""))
    operator_name = _get("HDE_OWNER_NAME")
    spreadsheet_id = _get("GOOGLE_SPREADSHEET_ID")
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "Ввод данных_2026")
    sheet_name_in_a = os.getenv("GOOGLE_SHEET_NAME_IN_A", operator_name)
    service_account_file = _get_google_service_account_file()
    service_account_path = Path(service_account_file)
    if not service_account_path.exists():
        raise RuntimeError(
            f"Google service account file not found: {service_account_path}"
        )

    artifacts = Path(os.getenv("REPORT_ARTIFACTS_DIR", "artifacts"))
    screenshots = artifacts / "screenshots"

    logger.info("Running operator report for %s", report_date)

    data = await get_operator_report_data(
        base_url=hde_base_url,
        login=hde_login,
        password=hde_password,
        report_date=report_date,
        operator_name=operator_name,
        screenshots_dir=screenshots,
    )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        append_operator_row,
        str(service_account_path),
        spreadsheet_id,
        worksheet_name,
        report_date,
        data,
        sheet_name_in_a,
    )

    date_str = report_date.strftime("%d.%m.%Y")
    return (
        f"✅ <b>Таблица заполнена!</b>\n\n"
        f"📅 Дата: <b>{date_str}</b>\n"
        f"👤 Оператор: <b>{data.operator}</b>\n"
        f"🎫 Закрыто заявок: <b>{data.tickets}</b>\n"
        f"⏱️ Среднее время выполнения: <b>{data.avg_time}</b>"
    )


async def run_weekly_summary(today: date | None = None) -> str:
    """Build weekly summary for the previous work week (prev Sunday..Thursday).

    Reads the Google Sheet directly — no HDE scraping. Intended to fire on
    Sunday mornings. Display range goes from prev Sunday through *today*
    (also Sunday), per product spec.
    """
    if today is None:
        today = date.today()
    start_date = today - timedelta(days=7)   # prev Sunday
    end_date = today - timedelta(days=3)     # prev Thursday
    display_end = today

    spreadsheet_id = _get("GOOGLE_SPREADSHEET_ID")
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "Ввод данных_2026")
    operator_name = _get("HDE_OWNER_NAME")
    sheet_name_in_a = os.getenv("GOOGLE_SHEET_NAME_IN_A", operator_name)
    service_account_file = _get_google_service_account_file()
    service_account_path = Path(service_account_file)
    if not service_account_path.exists():
        raise RuntimeError(
            f"Google service account file not found: {service_account_path}"
        )

    loop = asyncio.get_running_loop()
    summary = await loop.run_in_executor(
        None,
        read_weekly_tickets,
        str(service_account_path),
        spreadsheet_id,
        worksheet_name,
        sheet_name_in_a,
        start_date,
        end_date,
        display_end,
    )

    avg = (summary.total_tickets // summary.work_days) if summary.work_days else 0
    range_str = f"{summary.start_date.strftime('%d.%m')}–{summary.display_end.strftime('%d.%m')}"
    return (
        f"📊 <b>Итоги прошлой рабочей недели ({range_str})</b>\n\n"
        f"🎫 Закрыто тикетов: <b>{summary.total_tickets}</b>\n"
        f"📅 Рабочих дней: <b>{summary.work_days}</b>\n"
        f"📈 В среднем в день: <b>{avg}</b>"
    )
