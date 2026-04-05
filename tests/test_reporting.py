from datetime import date

import pytest

from bot.reporting import runner
from bot.reporting.hde_playwright import OperatorReportData


def _set_base_report_env(monkeypatch, service_account_path: str = "") -> None:
    monkeypatch.setenv("HDE_API_BASE_URL", "https://hde.example.com/api/v2")
    monkeypatch.setenv("HDE_API_EMAIL", "bot@example.com")
    monkeypatch.setenv("HDE_OWNER_NAME", "Игорь Кравцов")
    monkeypatch.setenv("HDE_API_KEY", "api-key")
    monkeypatch.setenv("GOOGLE_SPREADSHEET_ID", "spreadsheet-id")
    monkeypatch.setenv("GOOGLE_WORKSHEET_NAME", "Sheet1")
    monkeypatch.delenv("GOOGLE_SHEET_NAME_IN_A", raising=False)
    if service_account_path:
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", service_account_path)
    else:
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_CREDENTIALS_FILE", raising=False)


def test_is_report_configured_requires_existing_service_account_file(monkeypatch, tmp_path):
    missing_file = tmp_path / "missing.json"
    _set_base_report_env(monkeypatch, str(missing_file))

    assert runner.is_report_configured() is False


def test_is_report_configured_accepts_legacy_credentials_alias(monkeypatch, tmp_path):
    legacy_file = tmp_path / "legacy.json"
    legacy_file.write_text("{}", encoding="utf-8")
    _set_base_report_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(legacy_file))

    assert runner.is_report_configured() is True


@pytest.mark.asyncio
async def test_run_report_uses_service_account_file(monkeypatch, tmp_path):
    service_account = tmp_path / "service-account.json"
    service_account.write_text("{}", encoding="utf-8")
    _set_base_report_env(monkeypatch, str(service_account))

    calls = {}

    async def fake_get_operator_report_data(**kwargs):
        calls["hde"] = kwargs
        return OperatorReportData(
            operator="Игорь Кравцов",
            tickets="7",
            avg_time="00:12:00",
        )

    def fake_append_operator_row(
        credentials_file,
        spreadsheet_id,
        worksheet_name,
        report_date,
        data,
        sheet_name_in_a,
    ):
        calls["google"] = {
            "credentials_file": credentials_file,
            "spreadsheet_id": spreadsheet_id,
            "worksheet_name": worksheet_name,
            "report_date": report_date,
            "data": data,
            "sheet_name_in_a": sheet_name_in_a,
        }

    monkeypatch.setattr(runner, "get_operator_report_data", fake_get_operator_report_data)
    monkeypatch.setattr(runner, "append_operator_row", fake_append_operator_row)

    result = await runner.run_report(date(2026, 4, 4))

    assert "Таблица заполнена" in result
    assert calls["hde"]["operator_name"] == "Игорь Кравцов"
    assert calls["google"]["credentials_file"] == str(service_account)
    assert calls["google"]["spreadsheet_id"] == "spreadsheet-id"
    assert calls["google"]["worksheet_name"] == "Sheet1"
    assert calls["google"]["sheet_name_in_a"] == "Игорь Кравцов"
