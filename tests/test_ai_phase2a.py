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
