"""Regression tests: a transient Google 5xx must not kill a report.

Root cause of the original bug (2026-09-06, Sun 08:31 MSK): the weekly summary
called ``worksheet.get_all_values()`` exactly once. Sheets answered
``APIError: [503]: The service is currently unavailable.`` — a transient
backend blip — and the whole summary was lost for the week, because
``_weekly_summary_done`` is claimed before the try block.
"""
from datetime import date

import gspread
import pytest

from bot.reporting import google_sheets, runner


class _FakeResponse:
    """Minimal stand-in for requests.Response, enough for gspread.APIError."""

    def __init__(self, code: int, message: str):
        self.status_code = code
        self._payload = {
            "error": {"code": code, "message": message, "status": "ERROR"}
        }

    def json(self):
        return self._payload


def _api_error(code: int, message: str = "boom") -> gspread.exceptions.APIError:
    return gspread.exceptions.APIError(_FakeResponse(code, message))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Backoff delays are real seconds — skip them in tests."""
    slept: list[float] = []
    monkeypatch.setattr(google_sheets.time, "sleep", slept.append)
    return slept


def test_retry_survives_a_transient_503(no_sleep):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _api_error(503, "The service is currently unavailable.")
        return "ok"

    assert google_sheets._retry_5xx(flaky) == "ok"
    assert calls["n"] == 3
    assert no_sleep == [1.0, 2.0]


def test_retry_passes_args_through(no_sleep):
    def fn(a, b=None):
        return (a, b)

    assert google_sheets._retry_5xx(fn, 1, b=2) == (1, 2)


def test_client_errors_are_not_retried(no_sleep):
    calls = {"n": 0}

    def forbidden():
        calls["n"] += 1
        raise _api_error(403, "The caller does not have permission")

    with pytest.raises(gspread.exceptions.APIError):
        google_sheets._retry_5xx(forbidden)
    assert calls["n"] == 1
    assert no_sleep == []


def test_retry_gives_up_and_reraises(no_sleep):
    calls = {"n": 0}

    def always_down():
        calls["n"] += 1
        raise _api_error(503, "The service is currently unavailable.")

    with pytest.raises(gspread.exceptions.APIError):
        google_sheets._retry_5xx(always_down)
    assert calls["n"] == google_sheets._RETRY_ATTEMPTS


# ---------------------------------------------------------------------------
# End-to-end through the sheet readers/writers
# ---------------------------------------------------------------------------

class _FakeWorksheet:
    def __init__(self, values, fail_times: int = 0):
        self._values = values
        self._fail = fail_times
        self.updates: list[tuple] = []

    def _maybe_fail(self):
        if self._fail > 0:
            self._fail -= 1
            raise _api_error(503, "The service is currently unavailable.")

    def get_all_values(self):
        self._maybe_fail()
        return self._values

    def col_values(self, col):
        self._maybe_fail()
        return [row[col - 1] for row in self._values]

    def update(self, range_name, values, value_input_option=None):
        self._maybe_fail()
        self.updates.append((range_name, values, value_input_option))


class _FakeSpreadsheet:
    def __init__(self, worksheet):
        self._worksheet = worksheet

    def worksheet(self, name):
        return self._worksheet


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _install_fake_gspread(monkeypatch, worksheet):
    monkeypatch.setattr(
        google_sheets.gspread,
        "service_account",
        lambda filename: _FakeClient(_FakeSpreadsheet(worksheet)),
    )


def test_read_weekly_tickets_survives_a_transient_503(monkeypatch, no_sleep):
    rows = [
        ["Игорь Кравцов", "31.08.2026", "", "5"],
        ["Игорь Кравцов", "01.09.2026", "", "7"],
        ["Другой Оператор", "01.09.2026", "", "99"],
        ["Игорь Кравцов", "05.09.2026", "", "3"],  # outside the window
    ]
    worksheet = _FakeWorksheet(rows, fail_times=2)
    _install_fake_gspread(monkeypatch, worksheet)

    summary = google_sheets.read_weekly_tickets(
        "creds.json",
        "sheet-id",
        "Ввод данных_2026",
        "Игорь Кравцов",
        date(2026, 8, 30),
        date(2026, 9, 3),
        date(2026, 9, 6),
    )

    assert summary.total_tickets == 12
    assert summary.work_days == 2


def test_append_operator_row_survives_a_transient_503(monkeypatch, no_sleep):
    from bot.reporting.hde_playwright import OperatorReportData

    worksheet = _FakeWorksheet([["Игорь Кравцов"]], fail_times=1)
    _install_fake_gspread(monkeypatch, worksheet)

    google_sheets.append_operator_row(
        "creds.json",
        "sheet-id",
        "Ввод данных_2026",
        date(2026, 9, 3),
        OperatorReportData(
            operator="Игорь Кравцов", tickets="7", avg_time="0:30", sla_violations="0"
        ),
        "Игорь Кравцов",
    )

    assert len(worksheet.updates) == 1
    range_name, values, _ = worksheet.updates[0]
    assert range_name == "A2:N2"
    assert values[0][1] == "03.09.2026"


# ---------------------------------------------------------------------------
# /weekly — manual re-run of a summary the scheduler already gave up on
# ---------------------------------------------------------------------------

class _DummyMessage:
    def __init__(self):
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        return _DummyWaitMessage()


class _DummyWaitMessage:
    async def delete(self):
        return None


class _DummyCommand:
    def __init__(self, args=None):
        self.args = args


@pytest.mark.asyncio
async def test_weekly_command_runs_the_summary(monkeypatch):
    from bot.handlers.report_commands import cmd_weekly

    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    calls: list = []

    async def fake_summary(today=None):
        calls.append(today)
        return "📊 итоги"

    monkeypatch.setattr(runner, "run_weekly_summary", fake_summary)

    message = _DummyMessage()
    await cmd_weekly(message, _DummyCommand())

    assert calls == [None]
    assert "📊 итоги" in message.answers[-1]


@pytest.mark.asyncio
async def test_weekly_command_accepts_a_date(monkeypatch):
    from bot.handlers.report_commands import cmd_weekly

    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    calls: list = []

    async def fake_summary(today=None):
        calls.append(today)
        return "📊 итоги"

    monkeypatch.setattr(runner, "run_weekly_summary", fake_summary)

    message = _DummyMessage()
    await cmd_weekly(message, _DummyCommand("2026-09-06"))

    assert calls == [date(2026, 9, 6)]


@pytest.mark.asyncio
async def test_weekly_command_rejects_a_bad_date(monkeypatch):
    from bot.handlers.report_commands import cmd_weekly

    monkeypatch.setattr(runner, "is_report_configured", lambda: True)

    async def fake_summary(today=None):
        raise AssertionError("must not run")

    monkeypatch.setattr(runner, "run_weekly_summary", fake_summary)

    message = _DummyMessage()
    await cmd_weekly(message, _DummyCommand("06.09.2026"))

    assert "формат" in message.answers[-1].lower()


@pytest.mark.asyncio
async def test_weekly_command_reports_the_error(monkeypatch):
    from bot.handlers.report_commands import cmd_weekly

    monkeypatch.setattr(runner, "is_report_configured", lambda: True)

    async def fake_summary(today=None):
        raise _api_error(503, "The service is currently unavailable.")

    monkeypatch.setattr(runner, "run_weekly_summary", fake_summary)

    message = _DummyMessage()
    await cmd_weekly(message, _DummyCommand())

    assert "503" in message.answers[-1]
