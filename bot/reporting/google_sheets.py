"""
Write operator report rows to Google Sheets via a service account.

Expected setup:
1. Enable Google Sheets API in Google Cloud.
2. Create a service account and download its JSON key.
3. Save the key locally, e.g. as ``secrets/google_service_account.json``.
4. Share the target spreadsheet with the service account email.
"""
from __future__ import annotations

import logging
from datetime import date

import gspread

from .hde_playwright import OperatorReportData

logger = logging.getLogger(__name__)

_TOTAL_COLS = 14  # A..N

_COL_NAME = 0
_COL_DATE = 1
_COL_TICKETS = 3
_COL_SLA_VIOL = 4
_COL_AVG_TIME = 10


def append_operator_row(
    credentials_file: str,
    spreadsheet_id: str,
    worksheet_name: str,
    report_date: date,
    data: OperatorReportData,
    sheet_name_in_a: str = "Кравцов Игорь",
) -> None:
    """Append one report row to the configured worksheet."""
    gc = gspread.service_account(filename=credentials_file)
    spreadsheet = gc.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name)

    row: list[str] = [""] * _TOTAL_COLS
    row[_COL_NAME] = sheet_name_in_a
    row[_COL_DATE] = report_date.strftime("%d.%m.%Y")
    row[_COL_TICKETS] = data.tickets
    row[_COL_SLA_VIOL] = data.sla_violations
    row[_COL_AVG_TIME] = data.avg_time

    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(
        "Appended row: worksheet=%s operator=%s date=%s tickets=%s sla_viol=%s avg_time=%s",
        worksheet_name,
        sheet_name_in_a,
        row[_COL_DATE],
        data.tickets,
        data.sla_violations,
        data.avg_time,
    )
