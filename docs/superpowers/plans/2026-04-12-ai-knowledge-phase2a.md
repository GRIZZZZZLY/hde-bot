# AI Knowledge System Phase 2A — Operations

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить обратную связь на 👍, расширенный `/aistatus`, bulk-импорт закрытых тикетов HDE и команду `/aireindex`.

**Architecture:** Пять файлов, нет новых модулей. Все команды добавляются в существующий `commands_router`. DB-функции добавляются в `bot/db.py`. HDE API расширяется методом `get_closed_tickets`. Deduplication через SHA256 content_hash в `knowledge_items`.

**Tech Stack:** Python 3.11+, aiogram 3.x, aiosqlite, aiohttp, hashlib (stdlib)

**Spec:** `C:\Users\Igor\.claude\plans\virtual-marinating-yao.md`

---

## File Map

| Файл | Действие |
|---|---|
| `bot/handlers/ai_feedback.py` | Toast "✅ Сохранено" на 👍 |
| `bot/db.py` | +`count_items_without_embedding()`, +`get_last_knowledge_item_date()` |
| `bot/hde_api.py` | +`get_closed_tickets(owner_id, limit)` |
| `bot/handlers/commands.py` | +`/aistatus`, +`/aiimport`, +`/aireindex`, обновить `/help` |
| `bot/main.py` | +3 BotCommand в `set_my_commands` |
| `tests/test_ai_phase2a.py` | Новый файл тестов |

---

## Task 1: Toast-подтверждение на 👍

**Files:**
- Modify: `bot/handlers/ai_feedback.py`

- [ ] **Step 1: Прочитать текущий cb_ai_good**

Открыть `bot/handlers/ai_feedback.py`, найти функцию `cb_ai_good`. Она выглядит примерно так:

```python
@router.callback_query(F.data == "ai:good")
async def cb_ai_good(callback: CallbackQuery) -> None:
    if not callback.message or not hasattr(callback.message, "message_thread_id"):
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    await callback.answer()          # ← эту строку меняем
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    ...
```

- [ ] **Step 2: Заменить `callback.answer()` на toast**

Найти вторую строку `await callback.answer()` в `cb_ai_good` (НЕ ту что в null guard) и заменить:

```python
    await callback.answer("✅ Сохранено в базу знаний", show_alert=False)
```

Единственное изменение — добавить строку в уже существующий вызов. Null guard в начале функции (`await callback.answer()` → return) оставить без изменений.

- [ ] **Step 3: Проверить импорт**

```bash
cd d:/HDE_bot && python -c "from bot.handlers.ai_feedback import cb_ai_good; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 4: Commit**

```bash
cd d:/HDE_bot && git add bot/handlers/ai_feedback.py && git commit -m "feat: show toast confirmation when 👍 feedback saved"
```

---

## Task 2: DB helper-функции для /aistatus

**Files:**
- Modify: `bot/db.py`
- Create/Modify: `tests/test_ai_phase2a.py`

- [ ] **Step 1: Написать тест (failing)**

Создать `tests/test_ai_phase2a.py`:

```python
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
    assert "T" in result  # ISO format
```

- [ ] **Step 2: Запустить — убедиться, что падает**

```bash
cd d:/HDE_bot && python -m pytest tests/test_ai_phase2a.py -v 2>&1 | tail -15
```
Ожидание: `FAILED` с `ImportError: cannot import name 'count_items_without_embedding'`

- [ ] **Step 3: Добавить функции в bot/db.py**

В конец `bot/db.py`, после `delete_ai_feedback_pending`, добавить:

```python
# ---------------------------------------------------------------------------
# AI status helpers
# ---------------------------------------------------------------------------

async def count_items_without_embedding() -> int:
    """Count knowledge_items that have no embedding blob (failed or pending)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items "
            "WHERE embedding IS NULL AND quality != 'bad'"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_last_knowledge_item_date() -> str | None:
    """Return ISO timestamp of the most recently created knowledge_item, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT MAX(created_at) FROM knowledge_items"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

```bash
cd d:/HDE_bot && python -m pytest tests/test_ai_phase2a.py -v 2>&1 | tail -15
```
Ожидание: `4 passed`

- [ ] **Step 5: Commit**

```bash
cd d:/HDE_bot && git add bot/db.py tests/test_ai_phase2a.py && git commit -m "feat: add count_items_without_embedding and get_last_knowledge_item_date to db"
```

---

## Task 3: HDE API — get_closed_tickets

**Files:**
- Modify: `bot/hde_api.py`
- Modify: `tests/test_ai_phase2a.py`

**Контекст:** HDEApiClient не имеет `_get` метода — каждый метод создаёт `aiohttp.ClientSession` напрямую. Ответ API: `data["data"]` — это dict (не list!), итерация через `.values()`. Пагинация через `data["meta"]["total_pages"]`.

- [ ] **Step 1: Добавить тест**

Добавить в конец `tests/test_ai_phase2a.py`:

```python
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_get_closed_tickets_pagination():
    """get_closed_tickets paginates until total_pages."""
    page1 = {
        "data": {
            "1": {"id": "101", "title": "Тикет 1", "owner_id": "42"},
            "2": {"id": "102", "title": "Тикет 2", "owner_id": "42"},
        },
        "meta": {"total_pages": 2},
    }
    page2 = {
        "data": {
            "3": {"id": "103", "title": "Тикет 3", "owner_id": "42"},
        },
        "meta": {"total_pages": 2},
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
```

- [ ] **Step 2: Запустить — убедиться, что падает**

```bash
cd d:/HDE_bot && python -m pytest tests/test_ai_phase2a.py::test_get_closed_tickets_pagination -v 2>&1 | tail -10
```
Ожидание: `FAILED` с `AttributeError: 'HDEApiClient' object has no attribute 'get_closed_tickets'`

- [ ] **Step 3: Добавить get_closed_tickets в bot/hde_api.py**

Найти в `bot/hde_api.py` метод `get_my_open_tickets` и добавить после него:

```python
    async def get_closed_tickets(
        self,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch up to `limit` closed tickets for the given owner_id.

        Returns list of raw ticket dicts from HDE API.
        """
        tickets: list[dict] = []
        page = 1

        while len(tickets) < limit:
            url = f"{self.base_url}/tickets/"
            params = {
                "owner_list": owner_id,
                "status_list": "closed",
                "page": str(page),
            }
            async with aiohttp.ClientSession(auth=self.auth) as session:
                async with session.get(url, params=params) as response:
                    data = await self._read_response(response)
                    if response.status >= 400:
                        message = (
                            self._extract_error_message(data)
                            or f"HDE API error {response.status}"
                        )
                        raise HDEApiError(message)

            if not isinstance(data, dict):
                break
            tickets_data = data.get("data", {})
            if not tickets_data:
                break

            for ticket_raw in tickets_data.values():
                if isinstance(ticket_raw, dict):
                    tickets.append(ticket_raw)

            meta = data.get("meta", {})
            total_pages = meta.get("total_pages", 1) if isinstance(meta, dict) else 1
            if page >= total_pages:
                break
            page += 1

        return tickets[:limit]
```

- [ ] **Step 4: Запустить тесты**

```bash
cd d:/HDE_bot && python -m pytest tests/test_ai_phase2a.py -v 2>&1 | tail -15
```
Ожидание: `5 passed`

- [ ] **Step 5: Commit**

```bash
cd d:/HDE_bot && git add bot/hde_api.py tests/test_ai_phase2a.py && git commit -m "feat: add get_closed_tickets to HDEApiClient"
```

---

## Task 4: Команда /aistatus

**Files:**
- Modify: `bot/handlers/commands.py`

**Зависимости:** Требует Task 2 (DB helpers).

- [ ] **Step 1: Добавить /aistatus в commands.py**

Найти в `bot/handlers/commands.py` строку:
```python
@router.message(Command("digest"))
```
Вставить ПЕРЕД ней:

```python
@router.message(Command("aistatus"))
async def cmd_aistatus(message: Message) -> None:
    """AI Knowledge System health overview."""
    from ..db import (
        count_knowledge_by_source,
        count_items_without_embedding,
        get_last_knowledge_item_date,
        get_setting,
    )
    from ..config import config
    from datetime import datetime, timezone

    counts = await count_knowledge_by_source()
    without_emb = await count_items_without_embedding()
    last_item_at = await get_last_knowledge_item_date()
    last_import_at = await get_setting("last_bulk_import_at", "")

    total = sum(counts.values())
    rag_status = "активен ✅" if config.gemini_api_key else "недоступен ❌ (нет Gemini key)"

    source_labels = {
        "feedback": "👍 feedback",
        "corrected": "✏️ corrected",
        "hde_closed": "📥 HDE import",
        "teamly": "🏢 Teamly",
        "doc": "📄 Внешние статьи",
        "macro": "🔧 Макросы HDE",
        "transcription": "🎙️ Транскрипции",
    }

    lines: list[str] = ["🧠 <b>AI Knowledge Status</b>", ""]
    lines.append(f"📚 База знаний: <b>{total} записей</b>")
    if without_emb:
        lines.append(f"   ⚠️ Без embedding: {without_emb} — /aireindex чтобы исправить")

    if counts:
        lines.append("")
        lines.append("По источникам:")
        for source, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            label = source_labels.get(source, source)
            lines.append(f"• {label}: <b>{cnt}</b>")

    lines.append("")
    lines.append(f"🔍 RAG: {rag_status}")

    if last_item_at:
        try:
            dt = datetime.fromisoformat(last_item_at)
            delta = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
            hours = int(delta.total_seconds() // 3600)
            if hours < 1:
                age = "менее часа назад"
            elif hours < 24:
                age = f"{hours} ч. назад"
            else:
                age = f"{delta.days} дн. назад"
            lines.append(f"🕐 Последнее пополнение: {age}")
        except Exception:
            pass

    if last_import_at:
        try:
            dt = datetime.fromisoformat(last_import_at)
            delta = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
            lines.append(f"📥 Последний bulk-импорт: {delta.days} дн. назад")
        except Exception:
            pass

    warnings: list[str] = []
    if not config.gemini_api_key:
        warnings.append("Gemini API key не настроен — RAG и embeddings недоступны")
    if without_emb:
        warnings.append(f"{without_emb} записей без embedding — /aireindex")
    if last_item_at:
        try:
            dt = datetime.fromisoformat(last_item_at)
            delta = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
            if delta.days >= 7:
                warnings.append("База не пополнялась 7+ дней")
        except Exception:
            pass

    if warnings:
        lines.append("")
        lines.append("⚠️ <b>Предупреждения:</b>")
        for w in warnings:
            lines.append(f"• {w}")

    await message.answer("\n".join(lines), parse_mode="HTML")

```

- [ ] **Step 2: Проверить импорт**

```bash
cd d:/HDE_bot && python -c "from bot.handlers.commands import cmd_aistatus; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 3: Commit**

```bash
cd d:/HDE_bot && git add bot/handlers/commands.py && git commit -m "feat: add /aistatus command with knowledge base health overview"
```

---

## Task 5: Команда /aiimport

**Files:**
- Modify: `bot/handlers/commands.py`

**Зависимости:** Требует Task 3 (get_closed_tickets). Synтаксис: `/aiimport [N] [owner_ids]`.

**Важно об HDE API:** поля тикета: `id` (строка), `title` (строка), `owner_id`. Ссылка строится как `{base_url.replace('/api/v2', '')}/tickets/{ticket_id}`.

- [ ] **Step 1: Добавить /aiimport в commands.py**

Вставить ПЕРЕД `@router.message(Command("digest"))`, после только что добавленного `/aistatus`:

```python
@router.message(Command("aiimport"))
async def cmd_aiimport(message: Message, command: CommandObject) -> None:
    """Bulk-import closed tickets from HDE into the knowledge base.

    Usage:
        /aiimport          — 50 own tickets
        /aiimport 100      — 100 own tickets
        /aiimport 50 456   — tickets of operator 456
        /aiimport 50 456,789 — tickets of two operators
    """
    import asyncio
    import hashlib
    import aiosqlite
    from ..config import config
    from ..db import DB_PATH, save_knowledge_item, set_setting
    from ..hde_api import HDEApiClient, HDEApiError
    from ..ai_summary import _build_history_text
    from ..knowledge.indexer import index_knowledge_item
    from datetime import datetime, timezone

    # Parse args
    raw = (command.args or "").strip().split()
    limit = 50
    owner_ids: list[str] = [config.hde_owner_id] if config.hde_owner_id else []

    if raw:
        if raw[0].isdigit():
            limit = int(raw[0])
            if len(raw) > 1:
                owner_ids = [x.strip() for x in raw[1].split(",") if x.strip()]
        else:
            owner_ids = [x.strip() for x in raw[0].split(",") if x.strip()]

    if not owner_ids:
        await message.answer(
            "⚠️ HDE_OWNER_ID не настроен и owner_id не указан.\n"
            "Использование: /aiimport [N] [owner_id,owner_id2]",
            parse_mode="HTML",
        )
        return

    if not config.has_hde_api_credentials():
        await message.answer("⚠️ HDE API не настроен (HDE_API_EMAIL / HDE_API_KEY).", parse_mode="HTML")
        return

    wait_msg = await message.answer(
        f"📥 Импортирую закрытые тикеты (до {limit} на оператора, операторов: {len(owner_ids)})...",
        parse_mode="HTML",
    )

    client = HDEApiClient()
    added = 0
    skipped = 0
    errors = 0

    for owner_id in owner_ids:
        try:
            tickets = await client.get_closed_tickets(owner_id, limit=limit)
        except HDEApiError as exc:
            errors += 1
            try:
                await wait_msg.edit_text(
                    f"❌ Ошибка HDE API для оператора {owner_id}: {exc}", parse_mode="HTML"
                )
            except Exception:
                pass
            continue

        for i, ticket_raw in enumerate(tickets, 1):
            ticket_id = str(ticket_raw.get("id") or "")
            ticket_title = str(ticket_raw.get("title") or "")

            if not ticket_id:
                errors += 1
                continue

            # Deduplication
            content_hash = hashlib.sha256(f"hde:{ticket_id}".encode()).hexdigest()
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT id FROM knowledge_items WHERE content_hash = ?", (content_hash,)
                ) as cur:
                    exists = await cur.fetchone()
            if exists:
                skipped += 1
                continue

            # Fetch full conversation
            try:
                info = await client.get_ticket_info(ticket_id)
                posts = await client.get_ticket_posts(ticket_id)
            except Exception as exc:
                logger.warning("Failed to fetch ticket %s: %s", ticket_id, exc)
                errors += 1
                await asyncio.sleep(0.5)
                continue

            history = _build_history_text(posts, info)
            if not history.strip():
                skipped += 1
                continue

            content = f"Тема: {ticket_title}\n\n{history}"

            # Index (embed + save)
            item_id = await index_knowledge_item(
                source="hde_closed",
                content=content,
                ticket_id=ticket_id,
                title=ticket_title,
                quality="good",
            )
            if item_id is not None:
                # Save content_hash for deduplication
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE knowledge_items SET content_hash = ? WHERE id = ?",
                        (content_hash, item_id),
                    )
                    await db.commit()
            else:
                # No Gemini key — save text only, index later with /aireindex
                item_id = await save_knowledge_item(
                    source="hde_closed",
                    content=content,
                    ticket_id=ticket_id,
                    title=ticket_title,
                    quality="good",
                    content_hash=content_hash,
                )
            added += 1

            if i % 5 == 0:
                try:
                    await wait_msg.edit_text(
                        f"📥 [{i}/{len(tickets)}] {ticket_title[:50]}...", parse_mode="HTML"
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.5)

    await set_setting("last_bulk_import_at", datetime.now(timezone.utc).isoformat())

    try:
        await wait_msg.delete()
    except Exception:
        pass

    await message.answer(
        f"✅ <b>Импорт завершён</b>\n\n"
        f"• Добавлено: <b>{added}</b>\n"
        f"• Пропущено (дубли): <b>{skipped}</b>\n"
        f"• Ошибок: <b>{errors}</b>",
        parse_mode="HTML",
    )

```

- [ ] **Step 2: Проверить импорт**

```bash
cd d:/HDE_bot && python -c "from bot.handlers.commands import cmd_aiimport; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 3: Commit**

```bash
cd d:/HDE_bot && git add bot/handlers/commands.py && git commit -m "feat: add /aiimport for bulk HDE closed tickets ingestion"
```

---

## Task 6: Команда /aireindex

**Files:**
- Modify: `bot/handlers/commands.py`

- [ ] **Step 1: Добавить /aireindex в commands.py**

Вставить ПЕРЕД `@router.message(Command("digest"))`:

```python
@router.message(Command("aireindex"))
async def cmd_aireindex(message: Message) -> None:
    """Regenerate embeddings for knowledge items that are missing them.

    Use after /aiimport when Gemini key was not configured,
    or after switching LLM providers.
    """
    from ..db import list_knowledge_items_without_embedding, update_knowledge_embedding
    from ..knowledge.indexer import embed_text
    from ..knowledge.store import embedding_to_bytes

    items = await list_knowledge_items_without_embedding()
    if not items:
        await message.answer("✅ Все записи уже проиндексированы.", parse_mode="HTML")
        return

    wait_msg = await message.answer(
        f"🔄 Переиндексирую {len(items)} записей...", parse_mode="HTML"
    )
    done = 0
    errors = 0
    for item_id, content in items:
        emb = await embed_text(content)
        if emb is None:
            errors += 1
            continue
        await update_knowledge_embedding(item_id, embedding_to_bytes(emb))
        done += 1

    try:
        await wait_msg.delete()
    except Exception:
        pass

    result = f"✅ <b>Переиндексировано: {done}</b> записей"
    if errors:
        result += f"\n⚠️ Ошибок: {errors} (нет Gemini key или API недоступен)"
    await message.answer(result, parse_mode="HTML")

```

- [ ] **Step 2: Проверить импорт**

```bash
cd d:/HDE_bot && python -c "from bot.handlers.commands import cmd_aireindex; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 3: Commit**

```bash
cd d:/HDE_bot && git add bot/handlers/commands.py && git commit -m "feat: add /aireindex to regenerate missing embeddings"
```

---

## Task 7: /help + bot/main.py

**Files:**
- Modify: `bot/handlers/commands.py` (обновить /help)
- Modify: `bot/main.py` (BotCommand)

- [ ] **Step 1: Обновить /help в commands.py**

В функции `cmd_help`, найти строку:
```python
        "/aiknowledge — статистика базы знаний AI\n"
```
Добавить ПОСЛЕ неё:
```python
        "/aistatus — статус AI Knowledge System (RAG, embedding, предупреждения)\n"
        "/aiimport [N] [owner_id] — bulk-импорт закрытых тикетов HDE\n"
        "/aireindex — переиндексировать записи без embedding\n"
```

- [ ] **Step 2: Добавить BotCommand в bot/main.py**

Найти в `_main_async`:
```python
        BotCommand(command="aiknowledge", description="Статистика базы знаний AI"),
```
Добавить ПОСЛЕ:
```python
        BotCommand(command="aistatus",   description="Статус AI Knowledge System"),
        BotCommand(command="aiimport",   description="Импорт закрытых тикетов HDE"),
        BotCommand(command="aireindex",  description="Переиндексировать embeddings"),
```

- [ ] **Step 3: Проверить импорт**

```bash
cd d:/HDE_bot && python -c "from bot.main import _build_dispatcher; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 4: Запустить все тесты**

```bash
cd d:/HDE_bot && python -m pytest tests/ -v --tb=short 2>&1 | tail -20
```
Ожидание: все новые тесты проходят; pre-existing failures не увеличились.

- [ ] **Step 5: Commit**

```bash
cd d:/HDE_bot && git add bot/handlers/commands.py bot/main.py && git commit -m "feat: add /aistatus /aiimport /aireindex to help text and bot commands"
```

---

## Checklist самопроверки

- [x] **Spec coverage:**
  - Toast на 👍 → Task 1 ✅
  - `count_items_without_embedding` / `get_last_knowledge_item_date` → Task 2 ✅
  - `get_closed_tickets` в HDEApiClient → Task 3 ✅
  - `/aistatus` с предупреждениями → Task 4 ✅
  - `/aiimport` с deduplication + multi-owner → Task 5 ✅
  - `/aireindex` → Task 6 ✅
  - /help + BotCommand → Task 7 ✅

- [x] **Нет плейсхолдеров:** все шаги содержат реальный код

- [x] **Типы консистентны:** 
  - `count_items_without_embedding() -> int` — Task 2, используется Task 4
  - `get_last_knowledge_item_date() -> str | None` — Task 2, используется Task 4
  - `get_closed_tickets(owner_id: str, limit: int) -> list[dict]` — Task 3, используется Task 5
  - `index_knowledge_item(...) -> int | None` — уже существует в `bot/knowledge/indexer.py`
  - `list_knowledge_items_without_embedding() -> list[tuple[int, str]]` — уже существует в `bot/db.py`
  - `update_knowledge_embedding(item_id: int, embedding: bytes)` — уже существует в `bot/db.py`
  - `embedding_to_bytes(embedding: np.ndarray) -> bytes` — уже существует в `bot/knowledge/store.py`
