"""Tests for general_channel hook integration in hde_webhook."""
import asyncio

import pytest
from unittest.mock import AsyncMock, patch
from aiohttp import web

import bot.hde_webhook as hde_webhook_module
from bot.hde_webhook import hde_webhook_handler


async def _drain_background() -> None:
    """Wait for the webhook's background processing task to finish."""
    while hde_webhook_module._background_tasks:
        await asyncio.gather(*list(hde_webhook_module._background_tasks))


@pytest.fixture
def mock_bot():
    """Create a mock Bot."""
    return AsyncMock()


@pytest.fixture
def mock_request(mock_bot):
    """Create a mock aiohttp Request with bot in app."""
    request = AsyncMock(spec=web.Request)
    request.app = {"bot": mock_bot}
    request.remote = "127.0.0.1"
    return request


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize DB tables before each test (processed_events table is required)."""
    import bot.db as db_module
    await db_module.init_db()


@pytest.mark.asyncio
async def test_webhook_calls_general_channel_hook_on_assigned_on_create(mock_request, mock_bot):
    """Test that assigned_on_create event triggers general_channel.on_assigned_on_create."""
    payload = {
        "event_type": "assigned_on_create",
        "ticket_id": "TKT-1",
        "unique_id": "A-1",
        "ticket_name": "Test ticket",
        "secret": "test_secret",
    }
    mock_request.json = AsyncMock(return_value=payload)

    with patch("bot.hde_webhook.HANDLERS") as mock_handlers, \
         patch("bot.hde_webhook.general_channel.on_assigned_on_create") as mock_gc_hook:
        mock_handlers.__getitem__.return_value = AsyncMock()
        mock_gc_hook.return_value = None
        response = await hde_webhook_handler(mock_request)
        await _drain_background()

    assert response.status == 200
    mock_gc_hook.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_calls_general_channel_hook_on_owner_changed(mock_request, mock_bot):
    """Test that owner_changed event triggers general_channel.on_owner_changed."""
    payload = {
        "event_type": "owner_changed",
        "ticket_id": "TKT-2",
        "unique_id": "B-2",
        "ticket_name": "Test ticket",
        "secret": "test_secret",
    }
    mock_request.json = AsyncMock(return_value=payload)

    with patch("bot.hde_webhook.HANDLERS") as mock_handlers, \
         patch("bot.hde_webhook.general_channel.on_owner_changed") as mock_gc_hook:
        mock_handlers.__getitem__.return_value = AsyncMock()
        mock_gc_hook.return_value = None
        response = await hde_webhook_handler(mock_request)
        await _drain_background()

    assert response.status == 200
    mock_gc_hook.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_calls_general_channel_hook_on_ticket_updated(mock_request, mock_bot):
    """Test that ticket_updated event triggers general_channel.on_ticket_updated."""
    payload = {
        "event_type": "ticket_updated",
        "ticket_id": "TKT-3",
        "unique_id": "C-3",
        "ticket_name": "Test ticket",
        "secret": "test_secret",
    }
    mock_request.json = AsyncMock(return_value=payload)

    with patch("bot.hde_webhook.HANDLERS") as mock_handlers, \
         patch("bot.hde_webhook.general_channel.on_ticket_updated") as mock_gc_hook:
        mock_handlers.__getitem__.return_value = AsyncMock()
        mock_gc_hook.return_value = None
        response = await hde_webhook_handler(mock_request)
        await _drain_background()

    assert response.status == 200
    mock_gc_hook.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_calls_general_channel_hook_on_ticket_closed(mock_request, mock_bot):
    """Test that ticket_closed event triggers general_channel.on_ticket_closed."""
    payload = {
        "event_type": "ticket_closed",
        "ticket_id": "TKT-4",
        "unique_id": "D-4",
        "ticket_name": "Test ticket",
        "secret": "test_secret",
    }
    mock_request.json = AsyncMock(return_value=payload)

    with patch("bot.hde_webhook.HANDLERS") as mock_handlers, \
         patch("bot.hde_webhook.general_channel.on_ticket_closed") as mock_gc_hook:
        mock_handlers.__getitem__.return_value = AsyncMock()
        mock_gc_hook.return_value = None
        response = await hde_webhook_handler(mock_request)
        await _drain_background()

    assert response.status == 200
    mock_gc_hook.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_general_channel_hook_failure_does_not_fail_request(mock_request, mock_bot):
    """Test that general_channel hook exception does not fail the webhook response."""
    payload = {
        "event_type": "assigned_on_create",
        "ticket_id": "TKT-5",
        "unique_id": "E-5",
        "ticket_name": "Test ticket",
        "secret": "test_secret",
    }
    mock_request.json = AsyncMock(return_value=payload)

    with patch("bot.hde_webhook.HANDLERS") as mock_handlers, \
         patch("bot.hde_webhook.general_channel.on_assigned_on_create") as mock_gc_hook:
        mock_handlers.__getitem__.return_value = AsyncMock()
        # General channel hook raises an exception
        mock_gc_hook.side_effect = Exception("General channel failed")
        response = await hde_webhook_handler(mock_request)
        await _drain_background()

    # Despite the general_channel hook failing, response should still be 200
    assert response.status == 200
