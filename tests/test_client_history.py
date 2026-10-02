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


@pytest.mark.asyncio
async def test_post_client_history_sends_message():
    """_post_client_history sends formatted message to topic when past tickets exist."""
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    mock_info = MagicMock()
    mock_info.client_id = 42
    mock_info.client_name = "Мария Иванова"

    mock_tickets = [
        {"id": 5, "subject": "Принтер", "status": "closed", "date_created": "2026-04-10 10:00"},
        {"id": 4, "subject": "TeamViewer", "status": "closed", "date_created": "2026-04-09 10:00"},
        {"id": 3, "subject": "Настройки", "status": "closed", "date_created": "2026-04-08 10:00"},
    ]

    mock_client = MagicMock()
    mock_client.get_ticket_info = AsyncMock(return_value=mock_info)
    mock_client.get_client_tickets = AsyncMock(return_value=mock_tickets)

    with patch("bot.hde_api.HDEApiClient", return_value=mock_client):
        from bot.topic_manager import _post_client_history
        await _post_client_history(mock_bot, chat_id=-100, topic_id=101, ticket_id="999")

    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert "Мария Иванова" in call_kwargs["text"]
    assert "3 обращений" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_post_client_history_skips_when_no_past_tickets():
    """_post_client_history sends nothing if all tickets are the current one."""
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    mock_info = MagicMock()
    mock_info.client_id = 42
    mock_info.client_name = "Петр"

    mock_tickets = [{"id": 999, "subject": "Current", "status": "open", "date_created": ""}]

    mock_client = MagicMock()
    mock_client.get_ticket_info = AsyncMock(return_value=mock_info)
    mock_client.get_client_tickets = AsyncMock(return_value=mock_tickets)

    with patch("bot.hde_api.HDEApiClient", return_value=mock_client):
        from bot.topic_manager import _post_client_history
        await _post_client_history(mock_bot, chat_id=-100, topic_id=101, ticket_id="999")

    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_post_client_history_swallows_api_error():
    """_post_client_history does not raise when HDE API fails."""
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    mock_client = MagicMock()
    mock_client.get_ticket_info = AsyncMock(side_effect=Exception("HDE down"))

    with patch("bot.hde_api.HDEApiClient", return_value=mock_client):
        from bot.topic_manager import _post_client_history
        await _post_client_history(mock_bot, chat_id=-100, topic_id=101, ticket_id="999")  # must not raise

    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_post_ticket_history_calls_client_history():
    """_post_ticket_history calls _post_client_history at the end."""
    import bot.topic_manager as tm

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with patch.object(tm, "_post_client_history", AsyncMock()) as mock_ch, \
         patch("bot.hde_api.HDEApiClient") as mock_api_cls, \
         patch("bot.topic_manager.config") as mock_cfg:
        mock_cfg.has_hde_api_credentials.return_value = True
        mock_cfg.group_chat_id = -100
        mock_api = MagicMock()
        mock_info = MagicMock()
        mock_api.get_ticket_info = AsyncMock(return_value=mock_info)
        mock_api.get_ticket_posts = AsyncMock(return_value=[])
        mock_api.get_ticket_comments = AsyncMock(return_value=[])
        mock_api_cls.return_value = mock_api

        await tm._post_ticket_history(mock_bot, ticket_id="123", chat_id=-100, topic_id=101)

    mock_ch.assert_called_once_with(mock_bot, -100, 101, "123", client=mock_api, info=mock_info)
