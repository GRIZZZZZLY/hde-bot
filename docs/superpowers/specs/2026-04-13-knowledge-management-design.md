# Knowledge Management — Design Spec

**Date:** 2026-04-13
**Status:** Approved

## Goal

Остановить неконтролируемый рост базы знаний, избавиться от дублей и устаревших данных, дать оператору инструмент для оценки качества базы через команду `/aimetrics`.

## Context

- База знаний: таблица `knowledge_items` (SQLite), ~847 записей в рабочей инсталляции
- Источники: `hde_closed` (bulk-import), `implicit_good` (автоответы), `implicit_corrected`, `feedback`
- Текущая проблема: нет дедупликации при reimport, нет expiry, `/aistatus` показывает только сырые счётчики
- Поле `content_hash` существует, но не используется — оставляем как есть

## Architecture

```
bot/db.py                  — +last_used_at колонка, upsert_knowledge_item(), dedup_knowledge_items()
bot/knowledge/store.py     — обновлять last_used_at в find_similar (throttle 24h)
bot/knowledge/indexer.py   — обновить index_knowledge_item() использовать upsert
bot/scheduler.py           — еженедельная задача expire_stale_knowledge()
bot/handlers/commands.py   — /aimetrics команда + callback-кнопки
bot/main.py                — зарегистрировать BotCommand /aimetrics
```

## Tech Stack

Python 3.10+, aiosqlite, aiogram 3.x, difflib (stdlib)

---

## Component 1 — Схема данных

### Изменение в `knowledge_items`

Добавить колонку:
```sql
ALTER TABLE knowledge_items ADD COLUMN last_used_at TEXT;
```

В `init_db` добавить migration-guard:
```python
try:
    await db.execute("ALTER TABLE knowledge_items ADD COLUMN last_used_at TEXT")
    await db.commit()
except Exception:
    pass  # column already exists
```

### Значения quality

Существующие: `'good'`, `'bad'`
Новое: `'expired'` — исключается из поиска наравне с `'bad'`, но семантически отличается (устарело, а не плохое качество).

Все запросы с `WHERE quality != 'bad'` обновить на `WHERE quality NOT IN ('bad', 'expired')`. Затронутые места: `list_all_knowledge_embeddings` в `db.py`, `fts_search_knowledge` в `db.py`, любые другие SELECT из `knowledge_items` с фильтром по quality.

---

## Component 2 — Дедупликация

### При импорте `hde_closed` (`/aiimport`)

Новая функция `upsert_knowledge_item()` в `bot/db.py`:

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
    """Insert or update knowledge item by ticket_id+source. Returns (id, created)."""
```

Логика:
- Если `ticket_id` непустой и существует запись с тем же `ticket_id` и `source` → UPDATE `content`, `title`, `embedding=NULL` (триггер на переиндексацию), `created_at=now()`
- Иначе → INSERT как сейчас
- Возвращает `(id, True)` если создан, `(id, False)` если обновлён

`/aiimport` в `commands.py` использует `upsert_knowledge_item` вместо `save_knowledge_item`.

### При сохранении `implicit_good`

В `topic_manager.py` в функции `_implicit_feedback`, перед вызовом `index_knowledge_item`:

```python
# Удалить старый implicit_good для того же тикета
await db.delete_knowledge_item_by_ticket(
    ticket_id=pending["ticket_id"],
    source="implicit_good",
)
```

Новая функция `delete_knowledge_item_by_ticket(ticket_id, source)` в `bot/db.py`.

### Кнопка "🧹 Дедуп" в `/aimetrics`

Вызывает `dedup_knowledge_items()` в `bot/db.py`:

```python
async def dedup_knowledge_items() -> int:
    """Mark duplicate items (same ticket_id+source, keep newest) as quality='bad'.
    Returns count of marked duplicates."""
```

Логика: GROUP BY `ticket_id, source`, для каждой группы с count > 1 — оставить запись с максимальным `id`, остальные пометить `quality='bad'`. Не трогать записи где `ticket_id IS NULL` или пустой.

---

## Component 3 — Expiry (устаревание)

### Константа

В `bot/scheduler.py`:
```python
KNOWLEDGE_EXPIRY_DAYS = 180
```

### Функция в `bot/db.py`

```python
async def expire_stale_knowledge(expiry_days: int = 180) -> int:
    """Mark old unused hde_closed items as expired. Returns count marked."""
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

### Еженедельный запуск в `scheduler.py`

В основном цикле планировщика — добавить проверку раз в неделю (воскресенье, 03:00 МСК):

```python
if now.weekday() == 6 and now.hour == 0:  # UTC Sunday 00:00 = 03:00 MSK
    marked = await expire_stale_knowledge(KNOWLEDGE_EXPIRY_DAYS)
    if marked:
        logger.info("Expired %d stale knowledge items", marked)
```

### Обновление last_used_at в find_similar

В `bot/knowledge/store.py` после формирования результата:

```python
# Update last_used_at for returned items (throttled: only if NULL or >24h ago)
ids_to_update = [item.id for item, _ in result if item.id]
if ids_to_update:
    await _update_last_used_at(ids_to_update)
```

Новая функция `_update_last_used_at(ids)` в `bot/db.py`:

```python
async def update_knowledge_last_used(item_ids: list[int]) -> None:
    """Update last_used_at for items where it's NULL or older than 24 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    placeholders = ",".join("?" * len(item_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE knowledge_items SET last_used_at = datetime('now') "
            f"WHERE id IN ({placeholders}) "
            f"AND (last_used_at IS NULL OR last_used_at < ?)",
            (*item_ids, cutoff),
        )
        await db.commit()
```

---

## Component 4 — Команда `/aimetrics`

### Новые функции в `bot/db.py`

```python
async def get_knowledge_metrics() -> dict:
    """Return metrics dict for /aimetrics display."""
    # Returns:
    # {
    #   "by_source": {"hde_closed": N, "implicit_good": N, ...},
    #   "total": N,
    #   "expired_count": N,
    #   "no_embedding_count": N,
    #   "top_patterns": [{"equipment": ..., "problem_type": ..., "use_count": N}, ...],  # top 5
    #   "dead_items": [{"id": N, "title": ..., "created_at": ..., "source": ...}, ...],  # top 5
    # }
```

"Мёртвые" элементы: `quality='good'` AND `last_used_at IS NULL` AND `created_at < 30 дней назад`, сортировка по `created_at ASC` (самые старые первые).

### Хендлер в `bot/handlers/commands.py`

```python
@router.message(Command("aimetrics"))
async def cmd_aimetrics(message: Message) -> None:
    ...
```

Формат сообщения:
```
📊 База знаний: {total} записей
  • {hde_closed} — из закрытых тикетов HDE (/aiimport)
  • {implicit_good} — подтверждены операторами
  • {implicit_corrected} — исправления AI-ответов
  • {feedback} — ручной фидбек

Устарело (>{KNOWLEDGE_EXPIRY_DAYS} дн, не используются): {expired_count}
Без эмбеддинга: {no_embedding} → /aireindex

🔥 Топ-5 паттернов решений:
1. {equipment} — {problem_type} ({use_count} раз)
...

💀 Топ-5 элементов без использования:
1. [{created_at}] {title}...
...
```

Кнопки:
```python
InlineKeyboardBuilder()
  .button(text="🧹 Дедуп", callback_data="km_dedup")
  .button(text="🗑 Удалить устаревшие", callback_data="km_expire")
  .adjust(2)
```

### Callback-хендлеры в `bot/handlers/commands.py`

```python
@router.callback_query(F.data == "km_dedup")
async def cb_km_dedup(callback: CallbackQuery) -> None:
    count = await db.dedup_knowledge_items()
    await callback.answer(f"🧹 Помечено дублей: {count}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)

@router.callback_query(F.data == "km_expire")
async def cb_km_expire(callback: CallbackQuery) -> None:
    count = await expire_stale_knowledge(KNOWLEDGE_EXPIRY_DAYS)
    await callback.answer(f"🗑 Помечено устаревших: {count}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)
```

### `/aistatus` как алиас

```python
@router.message(Command("aistatus"))
async def cmd_aistatus(message: Message) -> None:
    await cmd_aimetrics(message)
```

### Регистрация в `bot/main.py`

```python
BotCommand(command="aimetrics", description="Статистика и управление базой знаний"),
```

---

## Что НЕ входит в этот спек

- Семантический дедуп по эмбеддингам (cosine > 0.95) — отдельная задача
- Expiry для `implicit_good` и других источников
- UI для просмотра и редактирования отдельных элементов базы

---

## Проверка (Verification)

1. `/aiimport` дважды подряд → счётчик `hde_closed` не растёт (upsert работает)
2. Оператор подтверждает ответ дважды на один тикет → в базе один `implicit_good` для этого тикета
3. Кнопка `🧹 Дедуп` → возвращает количество дублей, счётчик в `/aimetrics` снижается
4. Кнопка `🗑 Удалить устаревшие` → `expired_count` в `/aimetrics` увеличивается
5. После нескольких тикетов с RAG → `last_used_at` обновился у использованных элементов
6. `/aistatus` → открывает `/aimetrics` (алиас работает)
