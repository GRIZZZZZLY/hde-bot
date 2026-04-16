"""Tests for Priority 6: topic_media_cache GC."""
from __future__ import annotations

import aiosqlite
import pytest

import bot.db as db_module


@pytest.mark.asyncio
async def test_gc_removes_rows_older_than_cutoff(initialized_db):
    """Rows with created_at older than N hours are deleted; fresh rows kept."""
    await db_module.cache_topic_media(
        topic_id=1, message_id=10, media_group_id="mg1",
        attachment_kind="photo", file_id="file1",
    )
    await db_module.cache_topic_media(
        topic_id=1, message_id=11, media_group_id="mg1",
        attachment_kind="photo", file_id="file2",
    )

    # Backdate one row by 2 hours via raw SQL
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute(
            "UPDATE topic_media_cache SET created_at = datetime('now', '-2 hours') "
            "WHERE message_id = ?",
            (10,),
        )
        await db.commit()

    deleted = await db_module.gc_stale_media_cache(hours=1)
    assert deleted == 1

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT message_id FROM topic_media_cache") as cur:
            rows = await cur.fetchall()
    remaining = [r["message_id"] for r in rows]
    assert remaining == [11]


@pytest.mark.asyncio
async def test_gc_empty_returns_zero(initialized_db):
    """No rows → GC returns 0, no error."""
    deleted = await db_module.gc_stale_media_cache(hours=1)
    assert deleted == 0


@pytest.mark.asyncio
async def test_gc_keeps_fresh_rows(initialized_db):
    """Fresh (<1h old) rows are untouched."""
    await db_module.cache_topic_media(
        topic_id=1, message_id=10, media_group_id="mg1",
        attachment_kind="photo", file_id="file1",
    )
    deleted = await db_module.gc_stale_media_cache(hours=1)
    assert deleted == 0

    rows = await db_module.list_cached_topic_media_group(1, "mg1")
    assert len(rows) == 1
