# Knowledge Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Остановить рост базы знаний через дедупликацию по ticket_id, пометку устаревших `hde_closed`-записей и команду `/aimetrics` с кнопками управления.

**Architecture:** Пять независимых задач: (1) новые CRUD в db.py + миграция схемы, (2) дедупликация при импорте и в implicit feedback, (3) обновление `last_used_at` в find_similar, (4) еженедельный expiry в scheduler, (5) команда `/aimetrics`. Каждая задача самостоятельна и тестируема.

**Tech Stack:** Python 3.10+, aiosqlite, aiogram 3.x, difflib (stdlib), re (stdlib)

---

## File Map

| Файл | Изменения |
|---|---|
| `bot/db.py` | +`last_used_at` колонка, quality filter → `NOT IN`, +6 новых функций |
| `bot/knowledge/store.py` | `find_similar` → вызов `update_knowledge_last_used` после результата |
| `bot/knowledge/indexer.py` | `index_knowledge_item` → `upsert_knowledge_item` вместо `save_and_index` |
| `bot/topic_manager.py` | `_implicit_feedback` → удалять старый `implicit_good` перед индексацией |
| `bot/scheduler.py` | +константа `KNOWLEDGE_EXPIRY_DAYS`, +еженедельная задача expiry |
| `bot/handlers/commands.py` | +`cmd_aimetrics`, +`cb_km_dedup`, +`cb_km_expire`, `/aistatus` → алиас |
| `bot/main.py` | +`BotCommand("aimetrics", ...)` |
| `tests/test_knowledge_management.py` | новый файл тестов |

---

## Task 1: DB schema + CRUD функции

**Files:**
- Modify: `bot/db.py`
- Test: `tests/test_knowledge_management.py`

- [ ] **Step 1: Создать тестовый файл**

```python
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
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```bash
cd d:\HDE_bot && python -m pytest tests/test_knowledge_management.py -v 2>&1 | head -30
```

Ожидаем: ImportError / AttributeError — функции ещё не существуют.

- [ ] **Step 3: Добавить миграцию last_used_at в init_db**

Найти функцию `init_db` в `bot/db.py`. После всех CREATE TABLE, перед `await db.commit()` в конце, добавить migration guard:

```python
        # Migration: add last_used_at to knowledge_items if missing
        try:
            await db.execute(
                "ALTER TABLE knowledge_items ADD COLUMN last_used_at TEXT"
            )
        except Exception:
            pass  # column already exists
```

- [ ] **Step 4: Обновить все фильтры quality в db.py**

Найти все вхождения `quality != 'bad'` в `bot/db.py`:

```bash
grep -n "quality != " d:\HDE_bot\bot\db.py
```

Заменить каждое на `quality NOT IN ('bad', 'expired')`. Использовать Edit для каждого вхождения. Типичная замена:

```python
# было:
"WHERE embedding IS NOT NULL AND quality != 'bad'"
# стало:
"WHERE embedding IS NOT NULL AND quality NOT IN ('bad', 'expired')"
```

- [ ] **Step 5: Добавить upsert_knowledge_item в конец bot/db.py**

```python
async def upsert_knowledge_item(
    source: str,
    content: str,
    *,
    ticket_id: str = "",
    title: str = "",
    quality: str = "good",
    url: str = "",
    company_id: str = "",
    company_name: str = "",
) -> tuple[int, bool]:
    """Insert or update knowledge item by ticket_id+source. Returns (id, was_created).

    If ticket_id is provided and a row with same (ticket_id, source) exists:
    → UPDATE content, title, reset embedding=NULL (triggers re-embedding), update FTS.
    Otherwise → INSERT new row.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if ticket_id:
            async with db.execute(
                "SELECT id FROM knowledge_items WHERE ticket_id = ? AND source = ?",
                (ticket_id, source),
            ) as cur:
                row = await cur.fetchone()
            if row:
                item_id = row[0]
                await db.execute(
                    "UPDATE knowledge_items "
                    "SET content=?, title=?, quality=?, url=?, "
                    "company_id=?, company_name=?, embedding=NULL, "
                    "created_at=datetime('now') "
                    "WHERE id=?",
                    (content, title or None, quality, url or None,
                     company_id or None, company_name or None, item_id),
                )
                # Обновить FTS
                await db.execute(
                    "DELETE FROM knowledge_fts WHERE rowid=?", (item_id,)
                )
                await db.execute(
                    "INSERT INTO knowledge_fts(rowid, content) VALUES (?, ?)",
                    (item_id, content),
                )
                await db.commit()
                return item_id, False
        # INSERT
        cursor = await db.execute(
            "INSERT INTO knowledge_items "
            "(source, ticket_id, title, content, quality, url, company_id, company_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, ticket_id or None, title or None, content, quality,
             url or None, company_id or None, company_name or None),
        )
        item_id = cursor.lastrowid
        assert item_id is not None
        try:
            await db.execute(
                "INSERT OR IGNORE INTO knowledge_fts(rowid, content) VALUES (?, ?)",
                (item_id, content),
            )
        except Exception as exc:
            logger.warning("FTS insert failed for item %s: %s", item_id, exc)
        await db.commit()
        return item_id, True
```

- [ ] **Step 6: Добавить delete_knowledge_item_by_ticket**

```python
async def delete_knowledge_item_by_ticket(ticket_id: str, source: str) -> int:
    """Delete knowledge items by ticket_id and source. Also cleans FTS. Returns count deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM knowledge_items WHERE ticket_id = ? AND source = ?",
            (ticket_id, source),
        ) as cur:
            rows = await cur.fetchall()
        ids = [r[0] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            await db.execute(
                f"DELETE FROM knowledge_fts WHERE rowid IN ({placeholders})", ids
            )
            await db.execute(
                f"DELETE FROM knowledge_items WHERE id IN ({placeholders})", ids
            )
            await db.commit()
        return len(ids)
```

- [ ] **Step 7: Добавить dedup_knowledge_items**

```python
async def dedup_knowledge_items() -> int:
    """Mark duplicate items (same ticket_id+source, keep newest id) as quality='bad'.
    Returns count of newly marked duplicates."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id FROM knowledge_items
            WHERE ticket_id IS NOT NULL
              AND ticket_id != ''
              AND quality NOT IN ('bad', 'expired')
              AND id NOT IN (
                  SELECT MAX(id)
                  FROM knowledge_items
                  WHERE ticket_id IS NOT NULL AND ticket_id != ''
                  GROUP BY ticket_id, source
              )
            """
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return 0
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE knowledge_items SET quality='bad' WHERE id IN ({placeholders})", ids
        )
        await db.commit()
        return len(ids)
```

- [ ] **Step 8: Добавить expire_stale_knowledge**

```python
async def expire_stale_knowledge(expiry_days: int = 180) -> int:
    """Mark old unused hde_closed items as quality='expired'. Returns count marked."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=expiry_days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE knowledge_items
            SET quality = 'expired'
            WHERE source = 'hde_closed'
              AND quality = 'good'
              AND created_at < ?
              AND (last_used_at IS NULL OR last_used_at < ?)
            """,
            (cutoff, cutoff),
        )
        await db.commit()
        return cur.rowcount
```

- [ ] **Step 9: Добавить update_knowledge_last_used**

```python
async def update_knowledge_last_used(item_ids: list[int]) -> None:
    """Update last_used_at for items where it's NULL or older than 24 hours (throttled)."""
    if not item_ids:
        return
    from datetime import datetime, timezone, timedelta
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    placeholders = ",".join("?" * len(item_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE knowledge_items SET last_used_at = datetime('now') "
            f"WHERE id IN ({placeholders}) "
            f"AND (last_used_at IS NULL OR last_used_at < ?)",
            (*item_ids, cutoff_24h),
        )
        await db.commit()
```

- [ ] **Step 10: Добавить get_knowledge_metrics**

```python
async def get_knowledge_metrics() -> dict:
    """Return metrics for /aimetrics command."""
    from datetime import datetime, timezone, timedelta
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # by_source (только active)
        async with db.execute(
            "SELECT source, COUNT(*) FROM knowledge_items "
            "WHERE quality NOT IN ('bad', 'expired') GROUP BY source"
        ) as cur:
            by_source = dict(await cur.fetchall())
        # expired count
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE quality = 'expired'"
        ) as cur:
            expired_count = (await cur.fetchone())[0]
        # without embedding
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items "
            "WHERE quality NOT IN ('bad', 'expired') AND embedding IS NULL"
        ) as cur:
            no_embedding_count = (await cur.fetchone())[0]
        # top 5 solution_patterns by use_count
        async with db.execute(
            "SELECT equipment, problem_type, use_count "
            "FROM solution_patterns ORDER BY use_count DESC LIMIT 5"
        ) as cur:
            top_patterns = [
                {"equipment": r[0], "problem_type": r[1], "use_count": r[2]}
                for r in await cur.fetchall()
            ]
        # top 5 "dead" items: good, never used, older than 30 days
        async with db.execute(
            "SELECT id, title, created_at, source FROM knowledge_items "
            "WHERE quality = 'good' AND last_used_at IS NULL AND created_at < ? "
            "ORDER BY created_at ASC LIMIT 5",
            (cutoff_30d,),
        ) as cur:
            dead_items = [
                {"id": r[0], "title": r[1], "created_at": r[2], "source": r[3]}
                for r in await cur.fetchall()
            ]
    return {
        "by_source": by_source,
        "total": sum(by_source.values()),
        "expired_count": expired_count,
        "no_embedding_count": no_embedding_count,
        "top_patterns": top_patterns,
        "dead_items": dead_items,
    }
```

- [ ] **Step 11: Запустить тесты — убедиться что проходят**

```bash
cd d:\HDE_bot && python -m pytest tests/test_knowledge_management.py -v
```

Ожидаем: все тесты PASSED.

- [ ] **Step 12: Синтаксическая проверка**

```bash
python -c "import ast; ast.parse(open('bot/db.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 13: Commit**

```bash
git add bot/db.py tests/test_knowledge_management.py
git commit -m "feat: knowledge management DB — last_used_at, expiry, upsert, dedup, metrics"
```

---

## Task 2: Дедупликация в indexer.py и topic_manager.py

**Files:**
- Modify: `bot/knowledge/indexer.py`
- Modify: `bot/topic_manager.py`
- Test: `tests/test_knowledge_management.py`

- [ ] **Step 1: Добавить тест для дедупликации в indexer**

Добавить в `tests/test_knowledge_management.py`:

```python
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
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```bash
cd d:\HDE_bot && python -m pytest tests/test_knowledge_management.py::test_index_knowledge_item_upserts_by_ticket_id -v 2>&1 | head -20
```

- [ ] **Step 3: Прочитать bot/knowledge/indexer.py**

Найти функцию `index_knowledge_item`. Она вызывает `save_and_index` из `store.py`. Нам нужно:
1. Заменить вызов `save_and_index` на `upsert_knowledge_item` из db
2. Обновить embedding отдельным шагом

- [ ] **Step 4: Обновить index_knowledge_item в indexer.py**

Найти функцию `index_knowledge_item` и заменить её тело так, чтобы использовался upsert:

```python
async def index_knowledge_item(
    source: str,
    content: str,
    *,
    ticket_id: str = "",
    title: str = "",
    quality: str = "good",
    url: str = "",
    company_id: str = "",
    company_name: str = "",
) -> int | None:
    """Embed content and upsert into knowledge_items. Returns item id or None."""
    from . import db as _db  # avoid circular import
    # Upsert first to get/create the DB row
    try:
        item_id, _ = await _db.upsert_knowledge_item(
            source=source,
            content=content,
            ticket_id=ticket_id,
            title=title,
            quality=quality,
            url=url,
            company_id=company_id,
            company_name=company_name,
        )
    except Exception as exc:
        logger.warning("upsert_knowledge_item failed: %s", exc)
        return None
    # Embed and update (non-fatal)
    embedding = await embed_text(content, task_type="passage")
    if embedding is not None:
        try:
            from .store import embedding_to_bytes
            emb_bytes = embedding_to_bytes(embedding)
            async with aiosqlite.connect(_db.DB_PATH) as db:
                await db.execute(
                    "UPDATE knowledge_items SET embedding=? WHERE id=?",
                    (emb_bytes, item_id),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Embedding update failed for item %s: %s", item_id, exc)
    return item_id
```

Проверить что нужные импорты есть (aiosqlite должен быть уже импортирован, иначе добавить).

- [ ] **Step 5: Обновить topic_manager.py — удалять старый implicit_good**

Найти в `bot/topic_manager.py` строку вызова `index_knowledge_item` с `source="implicit_good"` (примерно строка 770). Перед этим вызовом добавить:

```python
        # Удалить старый implicit_good для этого тикета (один тикет = одна запись)
        try:
            await db.delete_knowledge_item_by_ticket(
                pending["ticket_id"], "implicit_good"
            )
        except Exception as exc:
            logger.warning("delete_knowledge_item_by_ticket failed: %s", exc)
        from .knowledge.indexer import index_knowledge_item
        await index_knowledge_item(
            source="implicit_good",
            ...  # остальные аргументы как были
        )
```

Внимание: перед редактированием прочитать `_implicit_feedback` полностью, чтобы не сломать контекст.

- [ ] **Step 6: Запустить все тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/test_knowledge_management.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 7: Синтаксическая проверка**

```bash
python -c "
import ast
for f in ['bot/knowledge/indexer.py', 'bot/topic_manager.py']:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 8: Commit**

```bash
git add bot/knowledge/indexer.py bot/topic_manager.py tests/test_knowledge_management.py
git commit -m "feat: dedup — upsert in index_knowledge_item, replace old implicit_good"
```

---

## Task 3: last_used_at в find_similar

**Files:**
- Modify: `bot/knowledge/store.py`
- Test: `tests/test_knowledge_management.py`

- [ ] **Step 1: Добавить тест**

Добавить в `tests/test_knowledge_management.py`:

```python
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
```

- [ ] **Step 2: Запустить тест — убедиться что падает**

```bash
cd d:\HDE_bot && python -m pytest tests/test_knowledge_management.py::test_find_similar_updates_last_used_at -v 2>&1 | head -20
```

- [ ] **Step 3: Обновить find_similar в store.py**

Найти функцию `find_similar` в `bot/knowledge/store.py`. В конце функции, после формирования `result` и перед `return result`, добавить non-fatal вызов:

```python
    # Update last_used_at for returned items (throttled 24h, non-fatal)
    if result:
        returned_ids = [item.id for item, _ in result if item.id is not None]
        if returned_ids:
            try:
                from .. import db as _db
                await _db.update_knowledge_last_used(returned_ids)
            except Exception as exc:
                logger.warning("update_knowledge_last_used failed: %s", exc)

    return result
```

Убедиться что `logger` определён в store.py (он должен быть). Если нет, добавить в начало файла:
```python
import logging
logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Запустить все тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/test_knowledge_management.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 5: Синтаксическая проверка**

```bash
python -c "import ast; ast.parse(open('bot/knowledge/store.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 6: Commit**

```bash
git add bot/knowledge/store.py tests/test_knowledge_management.py
git commit -m "feat: track last_used_at in find_similar (throttled 24h)"
```

---

## Task 4: Еженедельный expiry в scheduler.py

**Files:**
- Modify: `bot/scheduler.py`

- [ ] **Step 1: Прочитать bot/scheduler.py**

Найти основной цикл планировщика (функция `run_scheduler` или аналогичная). Понять как определяется текущее время (`now`), и где добавлять проверки на день недели.

- [ ] **Step 2: Добавить константу и еженедельную задачу**

В начало файла `bot/scheduler.py` добавить константу после существующих импортов:

```python
KNOWLEDGE_EXPIRY_DAYS = 180
```

Найти место в основном цикле где есть проверки по времени (рядом с `_maybe_send_digest` или похожими). Добавить еженедельную задачу (воскресенье UTC 00:00 = 03:00 МСК):

```python
    # Еженедельная очистка устаревших записей базы знаний (воскресенье 00:xx UTC)
    if now.weekday() == 6 and now.hour == 0:
        try:
            from .db import expire_stale_knowledge
            marked = await expire_stale_knowledge(KNOWLEDGE_EXPIRY_DAYS)
            if marked:
                logger.info("Weekly expiry: marked %d stale knowledge items as expired", marked)
        except Exception as exc:
            logger.warning("Weekly knowledge expiry failed: %s", exc)
```

`now` должен быть `datetime` объектом. Проверь что это UTC или конвертируй при необходимости. В существующем коде найди как `now` получается (вероятно `datetime.now(timezone.utc)`).

- [ ] **Step 3: Синтаксическая проверка**

```bash
python -c "import ast; ast.parse(open('bot/scheduler.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 4: Запустить все тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/ -v 2>&1 | tail -10
```

Ожидаем: все PASSED, нет регрессий.

- [ ] **Step 5: Commit**

```bash
git add bot/scheduler.py
git commit -m "feat: weekly knowledge expiry in scheduler (hde_closed >180 days unused)"
```

---

## Task 5: Команда /aimetrics

**Files:**
- Modify: `bot/handlers/commands.py`
- Modify: `bot/main.py`

- [ ] **Step 1: Прочитать cmd_aistatus в commands.py**

Найти `cmd_aistatus` (примерно строка 382). Понять импорты и как построены другие команды рядом. Это нужно чтобы /aistatus превратить в алиас.

- [ ] **Step 2: Добавить cmd_aimetrics в commands.py**

В конец файла (после последней команды) добавить:

```python
@router.message(Command("aimetrics"))
async def cmd_aimetrics(message: Message) -> None:
    """Показать метрики базы знаний и кнопки управления."""
    from ..db import get_knowledge_metrics, KNOWLEDGE_EXPIRY_DAYS_LABEL
    from ..scheduler import KNOWLEDGE_EXPIRY_DAYS
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    metrics = await get_knowledge_metrics()
    by_source = metrics["by_source"]

    # Строки по источникам
    source_lines = []
    labels = {
        "hde_closed": "из закрытых тикетов HDE (/aiimport)",
        "implicit_good": "подтверждены операторами",
        "implicit_corrected": "исправления AI-ответов",
        "feedback": "ручной фидбек",
    }
    for src, label in labels.items():
        count = by_source.get(src, 0)
        if count:
            source_lines.append(f"  • {count:4d} — {label}")
    other_sources = {k: v for k, v in by_source.items() if k not in labels}
    for src, count in other_sources.items():
        source_lines.append(f"  • {count:4d} — {src}")

    sources_text = "\n".join(source_lines) if source_lines else "  (пусто)"

    # Паттерны
    patterns_text = ""
    if metrics["top_patterns"]:
        lines = []
        for i, p in enumerate(metrics["top_patterns"], 1):
            eq = p["equipment"] or "Без бренда"
            lines.append(f"{i}. {eq} — {p['problem_type']} ({p['use_count']} раз)")
        patterns_text = "\n🔥 <b>Топ-5 паттернов решений:</b>\n" + "\n".join(lines)

    # Мёртвые элементы
    dead_text = ""
    if metrics["dead_items"]:
        lines = []
        for p in metrics["dead_items"]:
            date = (p["created_at"] or "")[:10]
            title = (p["title"] or "без заголовка")[:60]
            lines.append(f"[{date}] {title}")
        dead_text = "\n💀 <b>Топ-5 без использования:</b>\n" + "\n".join(lines)

    # Предупреждения
    warnings = []
    if metrics["expired_count"]:
        warnings.append(
            f"⚠️ Устарело (&gt;{KNOWLEDGE_EXPIRY_DAYS} дн, не используются): "
            f"<b>{metrics['expired_count']}</b>"
        )
    if metrics["no_embedding_count"]:
        warnings.append(
            f"⚠️ Без эмбеддинга: <b>{metrics['no_embedding_count']}</b> → /aireindex"
        )
    warnings_text = ("\n" + "\n".join(warnings)) if warnings else ""

    text = (
        f"📊 <b>База знаний:</b> {metrics['total']} записей\n"
        f"{sources_text}"
        f"{warnings_text}"
        f"{patterns_text}"
        f"{dead_text}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Дедуп", callback_data="km_dedup")
    builder.button(text="🗑 Удалить устаревшие", callback_data="km_expire")
    builder.adjust(2)

    await message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())


@router.callback_query(F.data == "km_dedup")
async def cb_km_dedup(callback: CallbackQuery) -> None:
    from ..db import dedup_knowledge_items
    count = await dedup_knowledge_items()
    await callback.answer(f"🧹 Помечено дублей: {count}", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data == "km_expire")
async def cb_km_expire(callback: CallbackQuery) -> None:
    from ..db import expire_stale_knowledge
    from ..scheduler import KNOWLEDGE_EXPIRY_DAYS
    count = await expire_stale_knowledge(KNOWLEDGE_EXPIRY_DAYS)
    await callback.answer(f"🗑 Помечено устаревших: {count}", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
```

Убедиться что `F` импортирован (из aiogram.filters): обычно уже есть в файле, проверить через `grep -n "^from aiogram" bot/handlers/commands.py`.

- [ ] **Step 3: Заменить cmd_aistatus на алиас**

Найти функцию `cmd_aistatus` в commands.py. Заменить её тело на вызов `cmd_aimetrics`:

```python
@router.message(Command("aistatus"))
async def cmd_aistatus(message: Message) -> None:
    """Алиас для /aimetrics (обратная совместимость)."""
    await cmd_aimetrics(message)
```

Убедись что `cmd_aimetrics` определена ВЫШЕ `cmd_aistatus` в файле, иначе переставь порядок.

- [ ] **Step 4: Добавить BotCommand в main.py**

Найти список BotCommand в `bot/main.py` (рядом с `aistatus`, `aianalyze`). Добавить:

```python
        BotCommand(command="aimetrics", description="Статистика и управление базой знаний"),
```

И удалить или оставить `aistatus` в этом списке — лучше оставить оба:

```python
        BotCommand(command="aistatus",  description="Статус AI (алиас /aimetrics)"),
        BotCommand(command="aimetrics", description="Статистика и управление базой знаний"),
```

- [ ] **Step 5: Синтаксическая проверка**

```bash
python -c "
import ast
for f in ['bot/handlers/commands.py', 'bot/main.py']:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 6: Запустить все тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/ -v 2>&1 | tail -10
```

- [ ] **Step 7: Commit**

```bash
git add bot/handlers/commands.py bot/main.py
git commit -m "feat: /aimetrics command — knowledge base stats, dedup and expiry buttons"
```

---

## Финальная проверка

- [ ] Запустить полный тест-сьют:

```bash
cd d:\HDE_bot && python -m pytest tests/ -v
```

- [ ] Синтаксис всех изменённых файлов:

```bash
python -c "
import ast
files = [
    'bot/db.py',
    'bot/knowledge/store.py',
    'bot/knowledge/indexer.py',
    'bot/topic_manager.py',
    'bot/scheduler.py',
    'bot/handlers/commands.py',
    'bot/main.py',
]
for f in files:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] Git push:

```bash
git push origin main
```

---

## Что делать вручную после деплоя

1. `git pull && systemctl restart hde-bot`
2. Написать боту `/aimetrics` — убедиться что показывает статистику
3. Нажать "🧹 Дедуп" — посмотреть сколько дублей было
4. Следующее воскресенье — планировщик автоматически пометит устаревшие
