# tests/test_client_history.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_get_client_tickets_returns_list():
    """get_client_tickets returns list of raw ticket dicts, newest first, limited."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)
    mock_response.json = AsyncMock(return_value={
        "data": [
            {"id": 10, "subject": "Тикет 10", "status": "closed", "date_created": "2026-04-10 10:00"},
            {"id": 9,  "subject": "Тикет 9",  "status": "closed", "date_created": "2026-04-09 10:00"},
            {"id": 8,  "subject": "Тикет 8",  "status": "closed", "date_created": "2026-04-08 10:00"},
        ]
    })

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_response)

    with patch("bot.hde_api.aiohttp.ClientSession", return_value=mock_session):
        from bot.hde_api import HDEApiClient
        client = object.__new__(HDEApiClient)
        client.base_url = "https://example.com/api/v2"
        client.auth = None

        result = await client.get_client_tickets(client_id=42, limit=10)

    assert len(result) == 3
    assert result[0]["id"] == 10
    assert result[0]["subject"] == "Тикет 10"


@pytest.mark.asyncio
async def test_get_client_tickets_respects_limit():
    """get_client_tickets slices result to limit."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)
    mock_response.json = AsyncMock(return_value={
        "data": [{"id": i, "subject": f"T{i}", "status": "closed", "date_created": ""} for i in range(20)]
    })

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_response)

    with patch("bot.hde_api.aiohttp.ClientSession", return_value=mock_session):
        from bot.hde_api import HDEApiClient
        client = object.__new__(HDEApiClient)
        client.base_url = "https://example.com/api/v2"
        client.auth = None

        result = await client.get_client_tickets(client_id=42, limit=5)

    assert len(result) == 5


def test_format_client_history_basic():
    from bot.formatter import format_client_history
    result = format_client_history(
        client_name="Мария Иванова",
        total=7,
        recent_titles=["Не работает TouchScreen", "Сбросились настройки", "TeamViewer"],
        last_ticket_date="3 дня назад",
    )
    assert "Мария Иванова" in result
    assert "7 обращений" in result
    assert "Не работает TouchScreen" in result
    assert "TeamViewer" in result
    assert "3 дня назад" in result


def test_format_client_history_empty_returns_empty():
    from bot.formatter import format_client_history
    result = format_client_history(
        client_name="Иван",
        total=0,
        recent_titles=[],
        last_ticket_date=None,
    )
    assert result == ""


def test_format_client_history_single_ticket():
    from bot.formatter import format_client_history
    result = format_client_history(
        client_name="Петр",
        total=1,
        recent_titles=["Принтер не печатает"],
        last_ticket_date="сегодня",
    )
    assert "Принтер не печатает" in result
    assert "сегодня" in result
