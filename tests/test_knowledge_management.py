# tests/test_knowledge_management.py
import pytest
import aiosqlite
from datetime import datetime, timezone, timedelta
from bot import db as _db


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


# --- upsert_knowledge_item ---

@pytest.mark.asyncio
async def test_upsert_creates_new_item():
    await _db.init_db()
    item_id, created = await _db.upsert_knowledge_item(
        source="hde_closed", content="АТОЛ ошибка ОФД",
        ticket_id="123", title="Тест",
    )
    assert item_id > 0
    assert created is True


@pytest.mark.asyncio
async def test_upsert_updates_existing_by_ticket_id():
    await _db.init_db()
    id1, _ = await _db.upsert_knowledge_item(
        source="hde_closed", content="Старый контент", ticket_id="T1",
    )
    id2, created = await _db.upsert_knowledge_item(
        source="hde_closed", content="Новый контент", ticket_id="T1",
    )
    assert id1 == id2
    assert created is False
    # проверить что контент обновился
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute("SELECT content FROM knowledge_items WHERE id=?", (id1,)) as cur:
            row = await cur.fetchone()
    assert row[0] == "Новый контент"


@pytest.mark.asyncio
async def test_upsert_no_ticket_id_always_inserts():
    await _db.init_db()
    id1, _ = await _db.upsert_knowledge_item(source="feedback", content="Контент 1")
    id2, _ = await _db.upsert_knowledge_item(source="feedback", content="Контент 2")
    assert id1 != id2  # без ticket_id всегда INSERT


# --- delete_knowledge_item_by_ticket ---

@pytest.mark.asyncio
async def test_delete_knowledge_item_by_ticket():
    await _db.init_db()
    await _db.upsert_knowledge_item(
        source="implicit_good", content="Ответ оператора",
        ticket_id="T42",
    )
    deleted = await _db.delete_knowledge_item_by_ticket("T42", "implicit_good")
    assert deleted == 1
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE ticket_id='T42'",
        ) as cur:
            count = (await cur.fetchone())[0]
    assert count == 0


# --- dedup_knowledge_items ---

@pytest.mark.asyncio
async def test_dedup_marks_older_duplicates_as_bad():
    await _db.init_db()
    # Три записи с одним ticket_id+source — должны остаться только самая новая
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO knowledge_items (source, ticket_id, content, quality) VALUES (?,?,?,?)",
            ("hde_closed", "DUP1", "Старая 1", "good"),
        )
        await db.execute(
            "INSERT INTO knowledge_items (source, ticket_id, content, quality) VALUES (?,?,?,?)",
            ("hde_closed", "DUP1", "Старая 2", "good"),
        )
        await db.execute(
            "INSERT INTO knowledge_items (source, ticket_id, content, quality) VALUES (?,?,?,?)",
            ("hde_closed", "DUP1", "Новая", "good"),
        )
        await db.commit()
    marked = await _db.dedup_knowledge_items()
    assert marked == 2
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT content FROM knowledge_items WHERE ticket_id='DUP1' AND quality='good'",
        ) as cur:
            rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Новая"


# --- expire_stale_knowledge ---

@pytest.mark.asyncio
async def test_expire_stale_marks_old_unused():
    await _db.init_db()
    old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO knowledge_items (source, content, quality, created_at) VALUES (?,?,?,?)",
            ("hde_closed", "Старая запись", "good", old_date),
        )
        await db.commit()
    marked = await _db.expire_stale_knowledge(expiry_days=180)
    assert marked == 1


@pytest.mark.asyncio
async def test_expire_does_not_mark_recently_used():
    await _db.init_db()
    old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    recent_used = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO knowledge_items (source, content, quality, created_at, last_used_at) "
            "VALUES (?,?,?,?,?)",
            ("hde_closed", "Используемая запись", "good", old_date, recent_used),
        )
        await db.commit()
    marked = await _db.expire_stale_knowledge(expiry_days=180)
    assert marked == 0


@pytest.mark.asyncio
async def test_expire_does_not_touch_implicit_good():
    await _db.init_db()
    old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO knowledge_items (source, content, quality, created_at) VALUES (?,?,?,?)",
            ("implicit_good", "Ответ оператора", "good", old_date),
        )
        await db.commit()
    marked = await _db.expire_stale_knowledge(expiry_days=180)
    assert marked == 0


# --- update_knowledge_last_used ---

@pytest.mark.asyncio
async def test_update_last_used_sets_timestamp():
    await _db.init_db()
    item_id, _ = await _db.upsert_knowledge_item(
        source="hde_closed", content="Тест", ticket_id="LU1",
    )
    await _db.update_knowledge_last_used([item_id])
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT last_used_at FROM knowledge_items WHERE id=?", (item_id,)
        ) as cur:
            row = await cur.fetchone()
    assert row[0] is not None


@pytest.mark.asyncio
async def test_update_last_used_throttled_24h():
    await _db.init_db()
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO knowledge_items (source, content, quality, last_used_at) VALUES (?,?,?,?)",
            ("hde_closed", "Тест", "good", recent),
        )
        item_id = (await db.execute("SELECT last_insert_rowid()")).lastrowid
        await db.commit()
    await _db.update_knowledge_last_used([item_id])
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT last_used_at FROM knowledge_items WHERE id=?", (item_id,)
        ) as cur:
            row = await cur.fetchone()
    # должна остаться прежняя (не обновилась, т.к. < 24ч)
    assert row[0] == recent


# --- get_knowledge_metrics ---

@pytest.mark.asyncio
async def test_get_knowledge_metrics_structure():
    await _db.init_db()
    await _db.upsert_knowledge_item(source="hde_closed", content="Тест 1", ticket_id="M1")
    await _db.upsert_knowledge_item(source="implicit_good", content="Тест 2", ticket_id="M2")
    metrics = await _db.get_knowledge_metrics()
    assert "by_source" in metrics
    assert "total" in metrics
    assert "expired_count" in metrics
    assert "no_embedding_count" in metrics
    assert "top_patterns" in metrics
    assert "dead_items" in metrics
    assert metrics["total"] == 2
    assert metrics["by_source"].get("hde_closed") == 1


# --- Task 2: dedup integration ---
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_index_knowledge_item_upserts_by_ticket_id():
    """Повторный вызов с тем же ticket_id не создаёт дубликат."""
    await _db.init_db()
    # Мокаем embed_text чтобы не загружать модель
    with patch("bot.knowledge.indexer.embed_text", new=AsyncMock(return_value=None)):
        from bot.knowledge.indexer import index_knowledge_item
        id1 = await index_knowledge_item(
            source="hde_closed", content="Контент 1", ticket_id="IDX1"
        )
        id2 = await index_knowledge_item(
            source="hde_closed", content="Контент 2", ticket_id="IDX1"
        )
    assert id1 == id2  # тот же элемент, не дубликат
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE ticket_id='IDX1'"
        ) as cur:
            count = (await cur.fetchone())[0]
    assert count == 1


@pytest.mark.asyncio
async def test_implicit_good_replaces_old():
    """Новый implicit_good для того же тикета заменяет старый."""
    await _db.init_db()
    with patch("bot.knowledge.indexer.embed_text", new=AsyncMock(return_value=None)):
        from bot.knowledge.indexer import index_knowledge_item
        id1 = await index_knowledge_item(
            source="implicit_good", content="Старый ответ", ticket_id="IMP1"
        )
        # Симулируем что topic_manager удалил старый перед новым
        await _db.delete_knowledge_item_by_ticket("IMP1", "implicit_good")
        id2 = await index_knowledge_item(
            source="implicit_good", content="Новый ответ", ticket_id="IMP1"
        )
    assert id1 != id2  # разные ID (старый удалён, новый создан)
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE ticket_id='IMP1'"
        ) as cur:
            count = (await cur.fetchone())[0]
    assert count == 1


# --- Task 3: last_used_at tracking ---
import numpy as np
from bot.knowledge.store import find_similar


@pytest.mark.asyncio
async def test_find_similar_updates_last_used_at():
    await _db.init_db()
    emb = np.ones(4, dtype=np.float32)
    await _db.upsert_knowledge_item(
        source="hde_closed", content="АТОЛ ОФД", ticket_id="LU_T1"
    )
    # Проставить embedding напрямую
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "UPDATE knowledge_items SET embedding=? WHERE ticket_id='LU_T1'",
            (emb.tobytes(),),
        )
        await db.commit()
    # last_used_at должен быть NULL до поиска
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT last_used_at FROM knowledge_items WHERE ticket_id='LU_T1'"
        ) as cur:
            row = await cur.fetchone()
    assert row[0] is None

    await find_similar(emb, limit=1, query_text="")

    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT last_used_at FROM knowledge_items WHERE ticket_id='LU_T1'"
        ) as cur:
            row = await cur.fetchone()
    assert row[0] is not None  # обновился после поиска


# --- list_knowledge_content_hashes (bulk dedup for /aiimport) ---

@pytest.mark.asyncio
async def test_list_knowledge_content_hashes():
    await _db.init_db()
    # Пустая база — пустой set
    assert await _db.list_knowledge_content_hashes() == set()

    await _db.save_knowledge_item(
        source="hde_closed", content="A", ticket_id="H1", content_hash="hash_a",
    )
    await _db.save_knowledge_item(
        source="hde_closed", content="B", ticket_id="H2", content_hash="hash_b",
    )
    # Запись без хэша (NULL) не должна попадать в set
    await _db.save_knowledge_item(
        source="hde_closed", content="C", ticket_id="H3",
    )

    assert await _db.list_knowledge_content_hashes() == {"hash_a", "hash_b"}


# --- batch reindex (/aireindex) ---

@pytest.mark.asyncio
async def test_embed_texts_one_encode_call():
    """embed_texts must encode the whole list in a single model.encode call."""
    import numpy as np
    from unittest.mock import AsyncMock, MagicMock, patch
    from bot.knowledge import indexer

    fake_model = MagicMock()
    fake_model.encode = MagicMock(return_value=np.ones((3, 4), dtype=np.float32))
    with patch.object(indexer, "_get_model", new=AsyncMock(return_value=fake_model)):
        result = await indexer.embed_texts(["a", "b", "c"])

    assert fake_model.encode.call_count == 1
    assert result is not None and len(result) == 3
    texts_arg = fake_model.encode.call_args[0][0]
    assert all(t.startswith("passage: ") for t in texts_arg)


@pytest.mark.asyncio
async def test_embed_texts_none_on_failure():
    import numpy as np  # noqa: F401
    from unittest.mock import AsyncMock, MagicMock, patch
    from bot.knowledge import indexer

    fake_model = MagicMock()
    fake_model.encode = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(indexer, "_get_model", new=AsyncMock(return_value=fake_model)):
        result = await indexer.embed_texts(["a"])

    assert result is None


@pytest.mark.asyncio
async def test_update_knowledge_embeddings_batch():
    """One call writes all embeddings; no rows left without embedding."""
    await _db.init_db()
    id1, _ = await _db.upsert_knowledge_item(source="hde_closed", content="A", ticket_id="B1")
    id2, _ = await _db.upsert_knowledge_item(source="hde_closed", content="B", ticket_id="B2")

    await _db.update_knowledge_embeddings([(id1, b"\x00\x01"), (id2, b"\x02\x03")])

    assert await _db.list_knowledge_items_without_embedding() == []


@pytest.mark.asyncio
async def test_cmd_aireindex_uses_one_batch(monkeypatch):
    """/aireindex must embed all pending items in one embed_texts call."""
    import numpy as np
    from unittest.mock import AsyncMock
    import bot.knowledge.indexer as indexer
    from bot.handlers.commands import cmd_aireindex

    await _db.init_db()
    await _db.upsert_knowledge_item(source="hde_closed", content="A", ticket_id="R1")
    await _db.upsert_knowledge_item(source="hde_closed", content="B", ticket_id="R2")

    embs = [np.ones(4, dtype=np.float32), np.ones(4, dtype=np.float32)]
    embed_texts_mock = AsyncMock(return_value=embs)
    monkeypatch.setattr(indexer, "embed_texts", embed_texts_mock)

    message = AsyncMock()
    await cmd_aireindex(message)

    embed_texts_mock.assert_awaited_once()
    assert await _db.list_knowledge_items_without_embedding() == []
