# AI Knowledge Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать векторный индекс знаний, RAG-контекст в AI саммари и inline-кнопки обратной связи 👍/✏️/👎.

**Architecture:** SQLite хранит тексты + float32 embeddings (Gemini text-embedding-004, 768 dim). Cosine similarity на numpy находит топ-3 похожих примера при генерации саммари. Feedback кнопки под каждым саммари позволяют оператору оценивать и сохранять хорошие ответы.

**Tech Stack:** Python 3.11+, aiosqlite, numpy, aiohttp (Gemini Embedding API), aiogram 3.x

**Spec:** `docs/superpowers/specs/2026-04-11-ai-knowledge-rag-design.md`

---

## File Map

| Файл | Действие | Ответственность |
|---|---|---|
| `bot/db.py` | Modify | Новые таблицы + 5 helper функций |
| `requirements.txt` | Modify | Добавить numpy |
| `bot/knowledge/__init__.py` | Create | Пустой пакет |
| `bot/knowledge/store.py` | Create | KnowledgeItem + save + find_similar |
| `bot/knowledge/indexer.py` | Create | embed_text (Gemini API) + index_item |
| `bot/handlers/ai_feedback.py` | Create | Callbacks 👍/✏️/👎 + correction handler |
| `bot/ai_summary.py` | Modify | RAG retrieval + inline keyboard при отправке |
| `bot/topic_manager.py` | Modify | Отправка саммари с feedback кнопками |
| `bot/main.py` | Modify | Зарегистрировать ai_feedback router |
| `tests/test_knowledge_store.py` | Create | Тесты store + indexer |

---

## Task 1: DB — таблицы knowledge_items и ai_feedback_pending

**Files:**
- Modify: `bot/db.py` (функция `_create_tables` + 5 новых функций в конце файла)

- [ ] **Step 1: Добавить CREATE TABLE в _create_tables**

Найти в `bot/db.py` конец функции `_create_tables` (после `CREATE TABLE report_runs`), вставить перед `await db.commit()`:

```python
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source       TEXT NOT NULL,
                ticket_id    TEXT,
                title        TEXT,
                content      TEXT NOT NULL,
                embedding    BLOB,
                quality      TEXT NOT NULL DEFAULT 'good',
                url          TEXT,
                content_hash TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_feedback_pending (
                topic_id   INTEGER PRIMARY KEY,
                ticket_id  TEXT NOT NULL,
                history    TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL
            )
            """
        )
```

- [ ] **Step 2: Добавить helper функции в конец bot/db.py**

```python
# ---------------------------------------------------------------------------
# Knowledge items
# ---------------------------------------------------------------------------

@dataclass
class KnowledgeItem:
    id: int
    source: str
    ticket_id: Optional[str]
    title: Optional[str]
    content: str
    quality: str


async def save_knowledge_item(
    source: str,
    content: str,
    *,
    ticket_id: str = "",
    title: str = "",
    embedding: bytes | None = None,
    quality: str = "good",
    url: str = "",
    content_hash: str = "",
) -> int:
    """Insert a new knowledge item. Returns the new row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO knowledge_items
                (source, ticket_id, title, content, embedding, quality, url, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, ticket_id or None, title or None, content,
             embedding, quality, url or None, content_hash or None),
        )
        await db.commit()
        return cursor.lastrowid


async def update_knowledge_embedding(item_id: int, embedding: bytes) -> None:
    """Store the embedding blob for an existing knowledge item."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE knowledge_items SET embedding = ? WHERE id = ?",
            (embedding, item_id),
        )
        await db.commit()


async def list_knowledge_items_without_embedding() -> list[tuple[int, str]]:
    """Return (id, content) for rows missing an embedding."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, content FROM knowledge_items WHERE embedding IS NULL AND quality != 'bad'"
        ) as cur:
            return await cur.fetchall()


async def list_all_knowledge_embeddings() -> list[tuple[int, str, bytes]]:
    """Return (id, content, embedding) for all indexed items."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, content, embedding FROM knowledge_items "
            "WHERE embedding IS NOT NULL AND quality != 'bad'"
        ) as cur:
            return await cur.fetchall()


async def count_knowledge_by_source() -> dict[str, int]:
    """Return {source: count} statistics."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT source, COUNT(*) FROM knowledge_items GROUP BY source"
        ) as cur:
            rows = await cur.fetchall()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# AI feedback pending (awaiting ✏️ correction)
# ---------------------------------------------------------------------------

async def save_ai_feedback_pending(
    topic_id: int,
    ticket_id: str,
    history: str,
    title: str,
    expires_at: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO ai_feedback_pending
                (topic_id, ticket_id, history, title, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (topic_id, ticket_id, history, title, expires_at),
        )
        await db.commit()


async def get_ai_feedback_pending(topic_id: int) -> Optional[dict]:
    """Return pending correction state or None if expired/missing."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ticket_id, history, title, expires_at "
            "FROM ai_feedback_pending WHERE topic_id = ?",
            (topic_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    from datetime import datetime, timezone
    expires = datetime.fromisoformat(row[3])
    if datetime.now(timezone.utc) > expires:
        await delete_ai_feedback_pending(topic_id)
        return None
    return {"ticket_id": row[0], "history": row[1], "title": row[2]}


async def delete_ai_feedback_pending(topic_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM ai_feedback_pending WHERE topic_id = ?", (topic_id,)
        )
        await db.commit()
```

- [ ] **Step 3: Запустить импорт проверку**

```bash
python -c "from bot.db import save_knowledge_item, get_ai_feedback_pending; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 4: Commit**

```bash
git add bot/db.py
git commit -m "feat: add knowledge_items and ai_feedback_pending DB tables"
```

---

## Task 2: requirements.txt — добавить numpy

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Добавить numpy**

```
aiogram>=3.10,<4.0
aiohttp>=3.9
aiosqlite>=0.19
python-dotenv>=1.0
numpy>=1.26
```

- [ ] **Step 2: Установить**

```bash
pip install numpy>=1.26
```
Ожидание: `Successfully installed numpy-...`

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: add numpy for vector similarity search"
```

---

## Task 3: bot/knowledge/store.py — векторный поиск

**Files:**
- Create: `bot/knowledge/__init__.py`
- Create: `bot/knowledge/store.py`

- [ ] **Step 1: Создать bot/knowledge/__init__.py (пустой)**

```python
```

- [ ] **Step 2: Написать тест (failing)**

Создать `tests/test_knowledge_store.py`:

```python
import numpy as np
import pytest

from bot.knowledge.store import cosine_similarity, embedding_to_bytes, bytes_to_embedding


def test_cosine_similarity_identical():
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert cosine_similarity(a, a) == pytest.approx(1.0, abs=1e-5)


def test_cosine_similarity_orthogonal():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-5)


def test_embedding_roundtrip():
    original = np.random.rand(768).astype(np.float32)
    restored = bytes_to_embedding(embedding_to_bytes(original))
    np.testing.assert_array_almost_equal(original, restored)
```

- [ ] **Step 3: Запустить тест — убедиться что падает**

```bash
pytest tests/test_knowledge_store.py -v
```
Ожидание: `FAILED` с `ImportError: cannot import name 'cosine_similarity'`

- [ ] **Step 4: Реализовать bot/knowledge/store.py**

```python
"""Vector store: save and retrieve knowledge items by cosine similarity."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ..db import (
    DB_PATH,
    KnowledgeItem,
    list_all_knowledge_embeddings,
    save_knowledge_item,
    update_knowledge_embedding,
)

logger = logging.getLogger(__name__)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Return cosine similarity in [-1, 1]. Handles zero vectors safely."""
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-8:
        return 0.0
    return float(np.dot(a, b) / denom)


def embedding_to_bytes(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32).copy()


async def find_similar(
    query_embedding: np.ndarray,
    *,
    limit: int = 3,
) -> list[KnowledgeItem]:
    """Return top-N knowledge items most similar to query_embedding."""
    rows = await list_all_knowledge_embeddings()
    if not rows:
        return []

    scored: list[tuple[float, tuple]] = []
    for row_id, content, emb_bytes in rows:
        try:
            emb = bytes_to_embedding(emb_bytes)
        except Exception:
            continue
        score = cosine_similarity(query_embedding, emb)
        scored.append((score, (row_id, content)))

    scored.sort(key=lambda x: x[0], reverse=True)

    result = []
    for score, (row_id, content) in scored[:limit]:
        result.append(KnowledgeItem(
            id=row_id,
            source="",
            ticket_id=None,
            title=None,
            content=content,
            quality="good",
        ))
    return result


async def save_and_index(
    source: str,
    content: str,
    embedding: np.ndarray,
    *,
    ticket_id: str = "",
    title: str = "",
    quality: str = "good",
    url: str = "",
) -> int:
    """Save content + embedding in one call. Returns new item id."""
    emb_bytes = embedding_to_bytes(embedding)
    item_id = await save_knowledge_item(
        source=source,
        content=content,
        ticket_id=ticket_id,
        title=title,
        embedding=emb_bytes,
        quality=quality,
        url=url,
    )
    return item_id
```

- [ ] **Step 5: Запустить тест — убедиться что проходит**

```bash
pytest tests/test_knowledge_store.py -v
```
Ожидание: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add bot/knowledge/__init__.py bot/knowledge/store.py tests/test_knowledge_store.py
git commit -m "feat: add knowledge vector store with cosine similarity"
```

---

## Task 4: bot/knowledge/indexer.py — Gemini Embedding API

**Files:**
- Create: `bot/knowledge/indexer.py`
- Modify: `tests/test_knowledge_store.py` (добавить тест indexer)

- [ ] **Step 1: Написать тест (failing)**

Добавить в `tests/test_knowledge_store.py`:

```python
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_embed_text_returns_ndarray():
    mock_response = {
        "embedding": {"values": [0.1] * 768}
    }
    with patch("bot.knowledge.indexer.aiohttp.ClientSession") as mock_session_cls:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=mock_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_get = AsyncMock()
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_get)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        from bot.knowledge.indexer import embed_text
        result = await embed_text("тестовый текст")

    assert result is not None
    assert result.shape == (768,)
    assert result.dtype == np.float32
```

- [ ] **Step 2: Запустить тест — убедиться что падает**

```bash
pytest tests/test_knowledge_store.py::test_embed_text_returns_ndarray -v
```
Ожидание: `FAILED` с `ImportError`

- [ ] **Step 3: Реализовать bot/knowledge/indexer.py**

```python
"""Embed text via Gemini text-embedding-004 and index into knowledge store."""
from __future__ import annotations

import logging

import aiohttp
import numpy as np

from ..config import config
from .store import find_similar, save_and_index

logger = logging.getLogger(__name__)

_EMBEDDING_MODEL = "text-embedding-004"
_EMBEDDING_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_EMBEDDING_MODEL}:embedContent"
)


async def embed_text(text: str) -> np.ndarray | None:
    """Return 768-dim float32 embedding or None on failure."""
    if not config.gemini_api_key:
        return None
    payload = {
        "model": f"models/{_EMBEDDING_MODEL}",
        "content": {"parts": [{"text": text[:8000]}]},  # API limit
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _EMBEDDING_URL,
                json=payload,
                params={"key": config.gemini_api_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Embedding API error %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
    except Exception as exc:
        logger.warning("embed_text failed: %s", exc)
        return None

    values = data.get("embedding", {}).get("values", [])
    if not values:
        return None
    return np.array(values, dtype=np.float32)


async def index_knowledge_item(
    source: str,
    content: str,
    *,
    ticket_id: str = "",
    title: str = "",
    quality: str = "good",
    url: str = "",
) -> int | None:
    """Embed content and save to knowledge store. Returns item id or None."""
    embedding = await embed_text(content)
    if embedding is None:
        logger.warning("Could not embed knowledge item (source=%s), skipping", source)
        return None
    item_id = await save_and_index(
        source=source,
        content=content,
        embedding=embedding,
        ticket_id=ticket_id,
        title=title,
        quality=quality,
        url=url,
    )
    logger.info("Indexed knowledge item id=%d source=%s", item_id, source)
    return item_id


async def get_rag_context(
    ticket_title: str,
    history_tail: str,
    *,
    limit: int = 3,
) -> list[str]:
    """Return list of content strings for top-N similar knowledge items."""
    query = f"{ticket_title}\n{history_tail[-600:]}"
    embedding = await embed_text(query)
    if embedding is None:
        return []
    similar = await find_similar(embedding, limit=limit)
    return [item.content for item in similar]
```

- [ ] **Step 4: Запустить все тесты**

```bash
pytest tests/test_knowledge_store.py -v
```
Ожидание: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add bot/knowledge/indexer.py tests/test_knowledge_store.py
git commit -m "feat: add Gemini embedding indexer for knowledge items"
```

---

## Task 5: bot/handlers/ai_feedback.py — кнопки 👍 ✏️ 👎

**Files:**
- Create: `bot/handlers/ai_feedback.py`

- [ ] **Step 1: Реализовать bot/handlers/ai_feedback.py**

```python
"""Inline feedback buttons for AI summaries: 👍 good / ✏️ correct / 👎 bad."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..db import (
    delete_ai_feedback_pending,
    get_ai_feedback_pending,
    save_ai_feedback_pending,
)
from ..knowledge.indexer import index_knowledge_item

logger = logging.getLogger(__name__)
router = Router()

_TTL_HOURS = 24


def make_ai_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍", callback_data="ai:good"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data="ai:edit"),
        InlineKeyboardButton(text="👎", callback_data="ai:bad"),
    ]])


async def register_feedback_pending(
    topic_id: int,
    ticket_id: str,
    history: str,
    title: str,
) -> None:
    """Store pending feedback state so correction handler can pick it up."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_TTL_HOURS)
    ).isoformat()
    await save_ai_feedback_pending(topic_id, ticket_id, history, title, expires_at)


# ── Callbacks ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ai:good")
async def cb_ai_good(callback: CallbackQuery) -> None:
    topic_id = callback.message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if pending is None:
        return
    await delete_ai_feedback_pending(topic_id)
    content = f"Тема: {pending['title']}\n\n{pending['history']}"
    await index_knowledge_item(
        source="feedback",
        content=content,
        ticket_id=pending["ticket_id"],
        title=pending["title"],
        quality="good",
    )
    logger.info("Saved good example for ticket %s", pending["ticket_id"])


@router.callback_query(F.data == "ai:bad")
async def cb_ai_bad(callback: CallbackQuery) -> None:
    topic_id = callback.message.message_thread_id
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await delete_ai_feedback_pending(topic_id)
    logger.info("Marked summary as bad for topic %d", topic_id)


@router.callback_query(F.data == "ai:edit")
async def cb_ai_edit(callback: CallbackQuery) -> None:
    topic_id = callback.message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if pending is None:
        return
    await callback.message.answer(
        "✏️ <b>Введи правильный ответ клиенту</b> — я сохраню его как пример.\n"
        "<i>Следующее сообщение в этом топике будет сохранено.</i>",
        parse_mode="HTML",
    )


# ── Correction capture ───────────────────────────────────────────────────────

class _HasPendingCorrection(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.message_thread_id is None:
            return False
        if message.from_user is None or message.from_user.is_bot:
            return False
        pending = await get_ai_feedback_pending(message.message_thread_id)
        return pending is not None


@router.message(_HasPendingCorrection(), F.text.is_not(None))
async def capture_correction(message: Message) -> None:
    topic_id = message.message_thread_id
    pending = await get_ai_feedback_pending(topic_id)
    if pending is None:
        return
    await delete_ai_feedback_pending(topic_id)
    correction_text = message.text.strip()
    content = (
        f"Тема: {pending['title']}\n\n"
        f"{pending['history']}\n\n"
        f"Правильный ответ: {correction_text}"
    )
    await index_knowledge_item(
        source="feedback",
        content=content,
        ticket_id=pending["ticket_id"],
        title=pending["title"],
        quality="corrected",
    )
    await message.answer("✅ <b>Сохранено как исправленный пример</b>", parse_mode="HTML")
```

- [ ] **Step 2: Проверить импорт**

```bash
python -c "from bot.handlers.ai_feedback import router, make_ai_feedback_keyboard; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 3: Commit**

```bash
git add bot/handlers/ai_feedback.py
git commit -m "feat: add AI feedback handlers (good/bad/correct inline buttons)"
```

---

## Task 6: Обновить bot/ai_summary.py — добавить RAG контекст

**Files:**
- Modify: `bot/ai_summary.py`

- [ ] **Step 1: Обновить `_build_system_prompt` — принять rag_examples**

Заменить существующую функцию `_build_system_prompt`:

```python
def _build_system_prompt(ticket_title: str, rag_examples: list[str] | None = None) -> str:
    base = "Ты — ассистент технической поддержки.\n"
    if ticket_title:
        base += (
            f"Тема обращения: «{ticket_title}»\n\n"
            "Используй тему и примеры, чтобы предложить конкретный ответ, "
            "подходящий именно для этого типа проблемы.\n\n"
        )
    if rag_examples:
        examples_text = "\n\n---\n\n".join(rag_examples)
        base += (
            "Вот примеры хороших ответов из вашей поддержки:\n\n"
            f"{examples_text}\n\n"
            "---\n\n"
            "Теперь обработай новый тикет:\n\n"
        )
    return base + _FORMAT_INSTRUCTIONS
```

- [ ] **Step 2: Обновить `generate_ticket_summary` — добавить RAG retrieval**

Найти строку `system_text = _build_system_prompt(ticket_title)` и заменить блок от неё до `payload = {`:

```python
    # RAG: find similar examples from knowledge base
    rag_examples: list[str] = []
    try:
        from .knowledge.indexer import get_rag_context
        rag_examples = await get_rag_context(ticket_title, history)
    except Exception as exc:
        logger.warning("RAG context retrieval failed: %s", exc)

    system_text = _build_system_prompt(ticket_title, rag_examples or None)
```

- [ ] **Step 3: Проверить импорт**

```bash
python -c "from bot.ai_summary import generate_ticket_summary; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 4: Commit**

```bash
git add bot/ai_summary.py
git commit -m "feat: integrate RAG context into AI summary prompt"
```

---

## Task 7: Обновить bot/topic_manager.py — отправка с feedback кнопками

**Files:**
- Modify: `bot/topic_manager.py`

- [ ] **Step 1: Найти блок отправки саммари в `_post_ticket_history`**

Текущий код (после `if summary:`):
```python
    if summary:
        try:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=summary,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramAPIError as exc:
            logger.warning("Failed to post AI summary to topic %d: %s", topic_id, exc)
```

- [ ] **Step 2: Заменить на отправку с кнопками и сохранением pending**

```python
    if summary:
        from .handlers.ai_feedback import make_ai_feedback_keyboard, register_feedback_pending
        try:
            msg = await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=summary,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=make_ai_feedback_keyboard(),
            )
            # Build history text for potential saving
            from .hde_api import HDEApiClient, HDEApiError
            _history_for_fb = ""
            try:
                _client = HDEApiClient()
                _info_fb = await _client.get_ticket_info(ticket_id)
                _posts_fb = await _client.get_ticket_posts(ticket_id)
                from .ai_summary import _build_history_text
                _history_for_fb = _build_history_text(_posts_fb, _info_fb)
            except Exception:
                pass
            await register_feedback_pending(
                topic_id=topic_id,
                ticket_id=ticket_id,
                history=_history_for_fb,
                title=ticket_title,
            )
        except TelegramAPIError as exc:
            logger.warning("Failed to post AI summary to topic %d: %s", topic_id, exc)
```

> **Примечание:** `_build_history_text` уже вызывался выше в `_post_ticket_history`. Чтобы не делать второй запрос к HDE, нужно прокинуть `history` как параметр. Это делается в следующем шаге.

- [ ] **Step 3: Рефакторинг — передать history в блок отправки саммари**

Найти в `_post_ticket_history` строку `messages = format_ticket_history(posts, info)` и после неё сохранить `history` в переменную:

```python
    messages = format_ticket_history(posts, info)
    # Build plain-text history once for both logging and feedback
    from .ai_summary import _build_history_text as _bht
    _plain_history = _bht(posts, info)
```

Затем заменить в блоке отправки `await register_feedback_pending(...)`:
```python
            await register_feedback_pending(
                topic_id=topic_id,
                ticket_id=ticket_id,
                history=_plain_history,
                title=ticket_title,
            )
```

И убрать вторичный вызов HDE API (все 6 строк от `from .hde_api import` до `_history_for_fb = _build_history_text(...)`).

- [ ] **Step 4: Проверить импорт**

```bash
python -c "from bot.topic_manager import _post_ticket_history; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 5: Commit**

```bash
git add bot/topic_manager.py
git commit -m "feat: send AI summary with feedback buttons, register pending state"
```

---

## Task 8: Зарегистрировать router + обновить команды

**Files:**
- Modify: `bot/main.py`
- Modify: `bot/handlers/commands.py`

- [ ] **Step 1: Добавить ai_feedback router в bot/main.py**

Найти импорт:
```python
from .handlers.commands import router as commands_router
```
Добавить после:
```python
from .handlers.ai_feedback import router as ai_feedback_router
```

Найти в `_build_dispatcher`:
```python
    dp.include_router(commands_router)
```
Добавить перед ним:
```python
    dp.include_router(ai_feedback_router)
```

> **Важно:** `ai_feedback_router` должен быть зарегистрирован ДО `commands_router`, чтобы `_HasPendingCorrection` фильтр срабатывал раньше catch-all обработчика.

- [ ] **Step 2: Добавить /aiknowledge команду в commands.py**

Добавить перед `@router.message(Command("digest"))`:

```python
@router.message(Command("aiknowledge"))
async def cmd_aiknowledge(message: Message) -> None:
    """Show knowledge base statistics."""
    from ..db import count_knowledge_by_source
    counts = await count_knowledge_by_source()
    if not counts:
        await message.answer(
            "📚 <b>База знаний пуста</b>\n\nОценивай саммари кнопками 👍/✏️ чтобы накапливать примеры.",
            parse_mode="HTML",
        )
        return
    source_labels = {
        "feedback": "👍 Оценённые ответы",
        "corrected": "✏️ Исправленные ответы",
        "teamly": "🏢 Teamly KB",
        "doc": "📄 Внешние статьи",
        "transcription": "🎙️ Транскрипции звонков",
        "macro": "🔧 Макросы HDE",
    }
    total = sum(counts.values())
    lines = [f"📚 <b>База знаний: {total} записей</b>", ""]
    for source, count in sorted(counts.items(), key=lambda x: -x[1]):
        label = source_labels.get(source, source)
        lines.append(f"• {label}: <b>{count}</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")
```

- [ ] **Step 3: Добавить /aiknowledge в setMyCommands в main.py**

```python
        BotCommand(command="aiknowledge", description="Статистика базы знаний AI"),
```
(добавить после `aisummary`)

- [ ] **Step 4: Добавить в /help текст**

```python
        "/aiknowledge — статистика базы знаний AI\n"
```

- [ ] **Step 5: Проверить запуск**

```bash
python -c "from bot.main import _build_dispatcher; print('OK')"
```
Ожидание: `OK`

- [ ] **Step 6: Commit**

```bash
git add bot/main.py bot/handlers/commands.py
git commit -m "feat: register ai_feedback router and add /aiknowledge command"
```

---

## Task 9: Smoke test — интеграция

**Files:**
- Create: `tests/test_ai_feedback_flow.py`

- [ ] **Step 1: Написать smoke тест**

```python
"""Smoke test: feedback pending state lifecycle."""
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone

from bot.db import init_db, save_ai_feedback_pending, get_ai_feedback_pending, delete_ai_feedback_pending


@pytest.fixture(autouse=True)
def use_test_db(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.db.DB_PATH", str(tmp_path / "test.db"))


@pytest.mark.asyncio
async def test_feedback_pending_lifecycle():
    await init_db()
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    await save_ai_feedback_pending(
        topic_id=42,
        ticket_id="123",
        history="Клиент: помогите\nСотрудник: ок",
        title="Проблема с принтером",
        expires_at=expires,
    )

    pending = await get_ai_feedback_pending(42)
    assert pending is not None
    assert pending["ticket_id"] == "123"
    assert pending["title"] == "Проблема с принтером"

    await delete_ai_feedback_pending(42)
    assert await get_ai_feedback_pending(42) is None


@pytest.mark.asyncio
async def test_feedback_pending_expired():
    await init_db()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    await save_ai_feedback_pending(
        topic_id=99,
        ticket_id="456",
        history="...",
        title="...",
        expires_at=past,
    )

    pending = await get_ai_feedback_pending(99)
    assert pending is None  # expired → auto-deleted
```

- [ ] **Step 2: Запустить**

```bash
pytest tests/test_ai_feedback_flow.py -v
```
Ожидание: `2 passed`

- [ ] **Step 3: Запустить все тесты**

```bash
pytest tests/ -v --tb=short
```
Ожидание: все проходят (или pre-existing failures не увеличились)

- [ ] **Step 4: Final commit**

```bash
git add tests/test_ai_feedback_flow.py
git commit -m "test: add smoke tests for AI feedback pending lifecycle"
```

---

## Checklist самопроверки

- [x] **Spec coverage:**
  - `knowledge_items` таблица → Task 1 ✅
  - `ai_feedback_pending` таблица → Task 1 ✅
  - Векторный поиск (cosine similarity) → Task 3 ✅
  - Gemini Embedding API → Task 4 ✅
  - Кнопки 👍/✏️/👎 → Task 5 ✅
  - RAG в промпте → Task 6 ✅
  - Отправка саммари с кнопками → Task 7 ✅
  - `/aiknowledge` команда → Task 8 ✅
  - Тесты → Tasks 3, 4, 9 ✅

- [x] **Нет плейсхолдеров:** все шаги содержат реальный код

- [x] **Типы консистентны:** `KnowledgeItem` определён в `db.py` Task 1, используется в `store.py` Task 3

- [x] **Функции консистентны:**
  - `embed_text(text: str) -> np.ndarray | None` — определена Task 4, используется Task 6
  - `get_rag_context(title, history) -> list[str]` — определена Task 4, используется Task 6
  - `make_ai_feedback_keyboard() -> InlineKeyboardMarkup` — Task 5, используется Task 7
  - `register_feedback_pending(...)` — Task 5, используется Task 7
