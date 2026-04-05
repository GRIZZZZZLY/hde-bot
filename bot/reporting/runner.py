"""
Daily HDE → Google Sheets report pipeline.

Called from:
  - bot/scheduler.py       (auto-run at REPORT_SEND_HOUR_UTC every day)
  - bot/handlers/commands.py  (/report or /report YYYY-MM-DD)

Required .env vars:
  HDE_REPORT_PASSWORD        — web UI password (falls back to HDE_API_KEY if not set)
  GOOGLE_CREDENTIALS_FILE    — path to OAuth2 client secrets JSON
  GOOGLE_SPREADSHEET_ID      — spreadsheet ID from the Google Sheet URL
  GOOGLE_WORKSHEET_NAME      — sheet tab name (default: "Ввод данных_2026")
  GOOGLE_SHEET_NAME_IN_A     — value written to column A (default: "Кравцов Игорь")

Optional:
  REPORT_SEND_HOUR_UTC       — UTC hour for auto-run (default: 6 → 9:00 Moscow)
  REPORT_ARTIFACTS_DIR       — directory for screenshots (default: artifacts)
  GOOGLE_TOKEN_FILE          — where to store OAuth token (default: secrets/google_token.json)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from .google_sheets import append_operator_row
from .hde_playwright import get_operator_report_data

logger = logging.getLogger(__name__)


def _get(name: str, fallback: str | None = None) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    if fallback is not None:
        return fallback
    raise RuntimeError(
        f"Env var {name!r} is not set. Add it to .env to enable the daily report."
    )


def is_report_configured() -> bool:
    """Return True only when all required env vars are present."""
    password_ok = bool(
        os.getenv("HDE_REPORT_PASSWORD", "").strip()
        or os.getenv("HDE_API_KEY", "").strip()
    )
    credentials_ok = bool(os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip())
    spreadsheet_ok = bool(os.getenv("GOOGLE_SPREADSHEET_ID", "").strip())
    return password_ok and credentials_ok and spreadsheet_ok


async def run_report(report_date: date | None = None) -> str:
    """
    Run the full pipeline for *report_date* (defaults to yesterday).

    Returns a formatted status string for the Telegram message.
    Raises RuntimeError with a descriptive message on failure.
    """
    if report_date is None:
        report_date = date.today() - timedelta(days=1)

    hde_base_url = os.getenv("HDE_API_BASE_URL", "").replace("/api/v2", "").rstrip("/")
    if not hde_base_url:
        raise RuntimeError("HDE_API_BASE_URL is not configured")

    hde_login    = _get("HDE_API_EMAIL")
    hde_password = _get("HDE_REPORT_PASSWORD", os.getenv("HDE_API_KEY", ""))
    if not hde_password:
        raise RuntimeError("Neither HDE_REPORT_PASSWORD nor HDE_API_KEY is set")

    operator_name  = _get("HDE_OWNER_NAME")
    spreadsheet_id = _get("GOOGLE_SPREADSHEET_ID")
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "Ввод данных_2026")
    sheet_name_a   = os.getenv("GOOGLE_SHEET_NAME_IN_A", "Кравцов Игорь")
    credentials    = _get("GOOGLE_CREDENTIALS_FILE")
    token_file     = os.getenv("GOOGLE_TOKEN_FILE", "secrets/google_token.json")

    artifacts     = Path(os.getenv("REPORT_ARTIFACTS_DIR", "artifacts"))
    screenshots   = artifacts / "screenshots"

    logger.info("Running report for %s", report_date)

    # Step 1: get data from HDE UI
    data = await get_operator_report_data(
        base_url=hde_base_url,
        login=hde_login,
        password=hde_password,
        report_date=report_date,
        operator_name=operator_name,
        screenshots_dir=screenshots,
    )

    # Step 2: write to Google Sheets (sync, run in executor to not block event loop)
    await asyncio.get_event_loop().run_in_executor(
        None,
        append_operator_row,
        credentials,
        token_file,
        spreadsheet_id,
        worksheet_name,
        report_date,
        data,
        sheet_name_a,
    )

    date_str = report_date.strftime("%d.%m.%Y")
    return (
        f"✅ <b>Таблица заполнена!</b>\n\n"
        f"📅 Дата: <b>{date_str}</b>\n"
        f"👤 Оператор: <b>{data.operator}</b>\n"
        f"🎫 Закрыто заявок: <b>{data.tickets}</b>\n"
        f"⏱️ Среднее время выполнения: <b>{data.avg_time}</b>"
    )
