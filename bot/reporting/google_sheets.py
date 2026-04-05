"""
Write operator report row to Google Sheets via OAuth2.

Sheet: "Ввод данных_2026"  (spreadsheet ID from GOOGLE_SPREADSHEET_ID)
Column mapping (0-indexed):
  A (0)  — ФИО              → "Кравцов Игорь"
  B (1)  — Дата             → report_date  DD.MM.YYYY
  C (2)  — SLA Первая линия → (empty, filled manually)
  D (3)  — Закрыто тикетов  → data.tickets
  E-I    — (empty)
  J (9)  — SLA Стандарт     → data.avg_time
  K+     — (empty)

Setup (one time):
  1. console.cloud.google.com → project → Enable "Google Sheets API"
  2. APIs & Services → Credentials → Create → OAuth 2.0 Client IDs → Desktop app
  3. Download JSON → save as secrets/google_credentials.json
  4. On first bot start the browser will open for Google authorization.
     Token is saved to secrets/google_token.json and reused automatically.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import gspread

from .hde_playwright import OperatorReportData

logger = logging.getLogger(__name__)

# Total number of columns in the sheet so we always write a full row
_TOTAL_COLS = 14  # A..N

# Column indices (0-based)
_COL_NAME     = 0   # A: ФИО
_COL_DATE     = 1   # B: Дата
_COL_TICKETS  = 3   # D: Закрыто тикетов
_COL_SLA_VIOL = 4   # E: Из них нарушений SLA по времени ответа
_COL_AVG_TIME = 9   # J: SLA полного ответа Стандарт


def append_operator_row(
    credentials_file: str | Path,
    token_file: str | Path,
    spreadsheet_id: str,
    worksheet_name: str,
    report_date: date,
    data: OperatorReportData,
    sheet_name_in_a: str = "Кравцов Игорь",
) -> None:
    """
    Append one row to *worksheet_name* in the given spreadsheet.

    On the very first call the function opens a browser window for Google
    OAuth2 consent. After that the token in *token_file* is reused silently.
    """
    Path(token_file).parent.mkdir(parents=True, exist_ok=True)

    gc = gspread.oauth(
        credentials_filename=str(credentials_file),
        authorized_user_filename=str(token_file),
    )

    spreadsheet = gc.open_by_key(spreadsheet_id)
    ws = spreadsheet.worksheet(worksheet_name)

    date_str = report_date.strftime("%d.%m.%Y")

    # Build a full-width row so columns land in the right positions
    row: list[str] = [""] * _TOTAL_COLS
    row[_COL_NAME]     = sheet_name_in_a
    row[_COL_DATE]     = date_str
    row[_COL_TICKETS]  = data.tickets
    row[_COL_SLA_VIOL] = "0"
    row[_COL_AVG_TIME] = data.avg_time

    ws.append_row(row, value_input_option="USER_ENTERED")
    logger.info(
        "Appended row to '%s': name=%s date=%s tickets=%s avg_time=%s",
        worksheet_name, sheet_name_in_a, date_str, data.tickets, data.avg_time,
    )
