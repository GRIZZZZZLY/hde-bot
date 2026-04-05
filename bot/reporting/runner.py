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

from .google_sheets import append_operator_row
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
    return all(
        (
            bool(os.getenv("HDE_API_BASE_URL", "").strip()),
            bool(os.getenv("HDE_API_EMAIL", "").strip()),
            bool(os.getenv("HDE_OWNER_NAME", "").strip()),
            password_ok,
            bool(os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()),
            bool(google_key),
            Path(google_key).exists(),
        )
    )


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
