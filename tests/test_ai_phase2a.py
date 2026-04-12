"""Tests for AI Knowledge System Phase 2A DB helpers."""
from __future__ import annotations

import pytest

from bot.db import (
    init_db,
    save_knowledge_item,
    count_items_without_embedding,
    get_last_knowledge_item_date,
)


@pytest.fixture(autouse=True)
def use_test_db(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.db.DB_PATH", str(tmp_path / "test.db"))


@pytest.mark.asyncio
async def test_count_items_without_embedding_empty():
    await init_db()
    result = await count_items_without_embedding()
    assert result == 0


@pytest.mark.asyncio
async def test_count_items_without_embedding():
    await init_db()
    # One item without embedding
    await save_knowledge_item(source="test", content="hello", embedding=None)
    # One item with embedding
    await save_knowledge_item(source="test", content="world", embedding=b"\x00" * 32)
    result = await count_items_without_embedding()
    assert result == 1


@pytest.mark.asyncio
async def test_get_last_knowledge_item_date_empty():
    await init_db()
    result = await get_last_knowledge_item_date()
    assert result is None


@pytest.mark.asyncio
async def test_get_last_knowledge_item_date():
    await init_db()
    await save_knowledge_item(source="test", content="hello")
    result = await get_last_knowledge_item_date()
    assert result is not None
    assert "T" in result or "-" in result  # ISO or SQLite datetime format


from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_get_closed_tickets_pagination():
    """get_closed_tickets paginates until total_pages."""
    page1 = {
        "data": {
            "1": {"id": "101", "title": "Тикет 1", "owner_id": "42"},
            "2": {"id": "102", "title": "Тикет 2", "owner_id": "42"},
        },
        "pagination": {"total_pages": 2},
    }
    page2 = {
        "data": {
            "3": {"id": "103", "title": "Тикет 3", "owner_id": "42"},
        },
        "pagination": {"total_pages": 2},
    }

    call_count = 0

    async def fake_read_response(resp):
        nonlocal call_count
        call_count += 1
        return page1 if call_count == 1 else page2

    with patch("bot.hde_api.config") as mock_cfg, \
         patch("bot.hde_api.aiohttp.ClientSession") as mock_session_cls:
        mock_cfg.has_hde_api_credentials.return_value = True
        mock_cfg.hde_api_base_url = "https://hde.example.com/api/v2"
        mock_cfg.hde_api_email = "test@test.com"
        mock_cfg.hde_api_key = "key"
        mock_cfg.hde_owner_id = "42"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_get = MagicMock(return_value=mock_resp)
        mock_session = AsyncMock()
        mock_session.get = mock_get
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        from bot.hde_api import HDEApiClient
        client = HDEApiClient()
        client._read_response = fake_read_response

        tickets = await client.get_closed_tickets("42", limit=50)

    assert len(tickets) == 3
    assert tickets[0]["id"] == "101"
    assert tickets[2]["id"] == "103"
