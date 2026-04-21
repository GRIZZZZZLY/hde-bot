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
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import gspread
from gspread.utils import rowcol_to_a1

from .hde_playwright import OperatorReportData


@dataclass
class WeeklySummary:
    total_tickets: int
    work_days: int
    start_date: date
    end_date: date
    display_end: date

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

    # Write at the row after the last non-empty cell in column A.
    # Using append_row() leaves Sheets to auto-detect the "table" and misplaces
    # the row into shifted columns when the sheet has mixed layouts elsewhere.
    next_row = len(worksheet.col_values(1)) + 1
    start = rowcol_to_a1(next_row, 1)
    end = rowcol_to_a1(next_row, _TOTAL_COLS)
    worksheet.update(
        range_name=f"{start}:{end}",
        values=[row],
        value_input_option="USER_ENTERED",
    )
    logger.info(
        "Wrote row: worksheet=%s row=%s operator=%s date=%s tickets=%s sla_viol=%s avg_time=%s",
        worksheet_name,
        next_row,
        sheet_name_in_a,
        row[_COL_DATE],
        data.tickets,
        data.sla_violations,
        data.avg_time,
    )


def read_weekly_tickets(
    credentials_file: str,
    spreadsheet_id: str,
    worksheet_name: str,
    operator_name: str,
    start_date: date,
    end_date: date,
    display_end: date,
) -> WeeklySummary:
    """Sum tickets from column D for operator rows whose date falls into
    [start_date, end_date] (inclusive)."""
    gc = gspread.service_account(filename=credentials_file)
    spreadsheet = gc.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name)
    vals = worksheet.get_all_values()

    total = 0
    days: set[date] = set()
    target = operator_name.strip().lower()

    for row in vals:
        if len(row) <= _COL_TICKETS:
            continue
        if (row[_COL_NAME] or "").strip().lower() != target:
            continue
        raw_date = (row[_COL_DATE] or "").strip()
        try:
            row_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
        except ValueError:
            continue
        if not (start_date <= row_date <= end_date):
            continue
        tickets_raw = (row[_COL_TICKETS] or "").strip()
        if not tickets_raw:
            continue
        try:
            total += int(tickets_raw)
        except ValueError:
            continue
        days.add(row_date)

    return WeeklySummary(
        total_tickets=total,
        work_days=len(days),
        start_date=start_date,
        end_date=end_date,
        display_end=display_end,
    )
