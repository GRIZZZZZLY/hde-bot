# Prompt Optimizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Система ночной оптимизации промптов — три LLM-модели параллельно предлагают мутации `_FORMAT_INSTRUCTIONS`, лучшая оценивается на исторических тикетах, утром оператор получает Telegram-отчёт с кнопками ✅/❌.

**Architecture:** Новый модуль `bot/optimizer/` (agent, evaluator, mutations, llm_router) интегрируется с существующими `db.py`, `scheduler.py`, `ai_summary.py` и `ai_feedback.py`. Данные для оценки накапливаются в таблице `optimization_samples` при каждом фидбеке оператора. Оценщик делает Gemini-вызовы с кандидат-промптом, сравнивает результаты с реальными ответами оператора через difflib.

**Tech Stack:** Python 3.10+, aiosqlite, aiohttp (уже есть), httpx (уже есть), Gemini API (уже есть), Groq API (новый)

---

## File Map

| Файл | Что делается |
|---|---|
| `bot/db.py` | +`optimization_samples`, +`prompt_versions` таблицы, +6 CRUD функций |
| `bot/config.py` | +`groq_api_key: str` |
| `bot/optimizer/__init__.py` | пустой пакет |
| `bot/optimizer/llm_router.py` | `GeminiClient`, `GroqClient`, `LLMRouter` |
| `bot/optimizer/mutations.py` | `build_mutation_prompt()` |
| `bot/optimizer/evaluator.py` | `combined_score()`, `_generate_answer()` |
| `bot/optimizer/agent.py` | `run_optimizer(bot)` — главный цикл |
| `bot/ai_summary.py` | `get_active_format_instructions()`, `_build_system_prompt` принимает параметр |
| `bot/topic_manager.py` | В `_implicit_feedback`: сохранять запись в `optimization_samples` |
| `bot/handlers/ai_feedback.py` | В `cb_ai_good/bad/send_post`: сохранять запись в `optimization_samples` |
| `bot/scheduler.py` | +`_last_optimization_date`, триггер 23:00 UTC |
| `bot/handlers/commands.py` | +`cmd_aioptimize`, +`cb_opt_apply`, +`cb_opt_reject`, +`cb_opt_detail` |
| `bot/main.py` | +`BotCommand("aioptimize", ...)` |
| `tests/test_prompt_optimizer.py` | новый файл тестов |

---

## Task 1: DB schema + CRUD

**Files:**
- Modify: `bot/db.py`
- Create: `tests/test_prompt_optimizer.py`

- [ ] **Step 1: Создать тестовый файл**

```python
# tests/test_prompt_optimizer.py
import pytest
import aiosqlite
from bot import db as _db


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


# --- optimization_samples ---

@pytest.mark.asyncio
async def test_save_and_get_optimization_samples():
    await _db.init_db()
    await _db.save_optimization_sample(
        ticket_id="T1",
        title="Тест",
        history="История тикета",
        ai_answer="AI ответ",
        op_answer="Ответ оператора",
        outcome="accepted",
        confidence=75,
    )
    samples = await _db.get_optimization_samples(days=30)
    assert len(samples) == 1
    assert samples[0]["ticket_id"] == "T1"
    assert samples[0]["outcome"] == "accepted"
    assert samples[0]["ai_answer"] == "AI ответ"


@pytest.mark.asyncio
async def test_get_optimization_samples_filters_old():
    await _db.init_db()
    from datetime import datetime, timezone, timedelta
    old_date = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO optimization_samples (ticket_id, title, history, ai_answer, outcome, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("OLD", "старый", "история", "ответ", "accepted", old_date),
        )
        await db.commit()
    samples = await _db.get_optimization_samples(days=30)
    assert all(s["ticket_id"] != "OLD" for s in samples)


# --- prompt_versions ---

@pytest.mark.asyncio
async def test_get_active_prompt_returns_none_when_empty():
    await _db.init_db()
    result = await _db.get_active_prompt()
    assert result is None


@pytest.mark.asyncio
async def test_save_and_apply_prompt_version():
    await _db.init_db()
    version_id = await _db.save_prompt_version(
        content="Новая инструкция",
        score=0.82,
        proposed_by="llama",
    )
    assert version_id > 0

    await _db.apply_prompt_version(version_id)
    active = await _db.get_active_prompt()
    assert active == "Новая инструкция"


@pytest.mark.asyncio
async def test_apply_sets_others_rejected():
    await _db.init_db()
    id1 = await _db.save_prompt_version("Вариант A", 0.75, "gemini")
    id2 = await _db.save_prompt_version("Вариант B", 0.80, "llama")

    await _db.apply_prompt_version(id2)

    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT status FROM prompt_versions WHERE id=?", (id1,)
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == "rejected"


@pytest.mark.asyncio
async def test_reject_all_candidates():
    await _db.init_db()
    await _db.save_prompt_version("Кандидат", 0.70, "mixtral")
    await _db.reject_all_prompt_candidates()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM prompt_versions WHERE status='candidate'"
        ) as cur:
            count = (await cur.fetchone())[0]
    assert count == 0
```

- [ ] **Step 2: Запустить — убедиться что падают**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py -v 2>&1 | head -20
```

Ожидаем: ImportError — функции не существуют.

- [ ] **Step 3: Прочитать `bot/db.py` — найти конец файла и init_db**

Прочитать последние 30 строк `bot/db.py` и найти `init_db` функцию, чтобы добавить CREATE TABLE в правильное место.

- [ ] **Step 4: Добавить таблицы в `init_db` в `bot/db.py`**

В `init_db()`, рядом с другими `CREATE TABLE IF NOT EXISTS`, добавить:

```python
        await db.execute("""
            CREATE TABLE IF NOT EXISTS optimization_samples (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   TEXT NOT NULL,
                title       TEXT,
                history     TEXT NOT NULL,
                ai_answer   TEXT NOT NULL,
                op_answer   TEXT,
                outcome     TEXT NOT NULL,
                confidence  INTEGER,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT NOT NULL,
                score       REAL,
                proposed_by TEXT,
                status      TEXT NOT NULL DEFAULT 'candidate',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                applied_at  TEXT
            )
        """)
```

- [ ] **Step 5: Добавить 5 CRUD функций в конец `bot/db.py`**

```python
async def save_optimization_sample(
    ticket_id: str,
    title: str,
    history: str,
    ai_answer: str,
    outcome: str,
    *,
    op_answer: str | None = None,
    confidence: int | None = None,
) -> int:
    """Save a labelled operator feedback sample for prompt optimization."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO optimization_samples "
            "(ticket_id, title, history, ai_answer, op_answer, outcome, confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticket_id, title or "", history, ai_answer, op_answer, outcome, confidence),
        )
        await db.commit()
        return cursor.lastrowid


async def get_optimization_samples(days: int = 30) -> list[dict]:
    """Return optimization samples from the last N days."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, ticket_id, title, history, ai_answer, op_answer, outcome, confidence "
            "FROM optimization_samples WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_active_prompt() -> str | None:
    """Return content of the active prompt version, or None if none applied yet."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content FROM prompt_versions WHERE status='active' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def save_prompt_version(content: str, score: float | None, proposed_by: str) -> int:
    """Save a candidate prompt version. Returns its id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO prompt_versions (content, score, proposed_by, status) VALUES (?,?,?,?)",
            (content, score, proposed_by, "candidate"),
        )
        await db.commit()
        return cursor.lastrowid


async def apply_prompt_version(version_id: int) -> None:
    """Mark version as active, all others as rejected."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE prompt_versions SET status='rejected' WHERE status IN ('active', 'candidate')"
        )
        await db.execute(
            "UPDATE prompt_versions SET status='active', applied_at=datetime('now') WHERE id=?",
            (version_id,),
        )
        await db.commit()


async def reject_all_prompt_candidates() -> None:
    """Mark all candidate prompt versions as rejected."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE prompt_versions SET status='rejected' WHERE status='candidate'"
        )
        await db.commit()
```

- [ ] **Step 6: Запустить тесты — убедиться что проходят**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py -v
```

Ожидаем: 7 тестов PASSED.

- [ ] **Step 7: Синтаксическая проверка**

```bash
cd d:\HDE_bot && python -c "import ast; ast.parse(open('bot/db.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 8: Commit**

```bash
git add bot/db.py tests/test_prompt_optimizer.py
git commit -m "feat: optimizer DB — optimization_samples and prompt_versions tables"
```

---

## Task 2: Config + LLM Router

**Files:**
- Modify: `bot/config.py`
- Create: `bot/optimizer/__init__.py`
- Create: `bot/optimizer/llm_router.py`
- Test: `tests/test_prompt_optimizer.py`

- [ ] **Step 1: Добавить тесты для LLM Router**

Добавить в конец `tests/test_prompt_optimizer.py`:

```python
# --- LLM Router ---

from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_llm_router_complete_all_returns_responses():
    """LLMRouter.complete_all возвращает dict model→response, пропускает недоступные модели."""
    from bot.optimizer.llm_router import LLMRouter

    async def fake_complete(system, user):
        return "fake response"

    router = LLMRouter.__new__(LLMRouter)
    router.clients = {"gemini": AsyncMock(complete=fake_complete)}

    results = await router.complete_all(system="system", user="user")
    assert results == {"gemini": "fake response"}


@pytest.mark.asyncio
async def test_groq_client_skipped_when_no_api_key():
    """GroqClient.complete raises если нет ключа."""
    from bot.optimizer.llm_router import GroqClient
    client = GroqClient(model="llama-3.3-70b-versatile", api_key="")
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        await client.complete("system", "user")
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py::test_llm_router_complete_all_returns_responses -v 2>&1 | head -15
```

- [ ] **Step 3: Добавить `groq_api_key` в `bot/config.py`**

В dataclass `Config` после `deepgram_api_key: str` добавить:
```python
    groq_api_key: str
```

В `from_env()` после `deepgram_api_key=os.getenv(...)` добавить:
```python
            groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
```

- [ ] **Step 4: Создать `bot/optimizer/__init__.py`**

```python
"""Prompt optimizer — autonomous nightly improvement of FORMAT_INSTRUCTIONS."""
```

- [ ] **Step 5: Создать `bot/optimizer/llm_router.py`**

```python
"""Multi-LLM router for prompt mutation proposals."""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

import aiohttp

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class GeminiClient:
    """Thin wrapper around Gemini generateContent API."""

    _URL_TEMPLATE = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "{model}:generateContent"
    )

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key
        self._url = self._URL_TEMPLATE.format(model=model)

    async def complete(self, system: str, user: str) -> str:
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1000},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._url,
                params={"key": self.api_key},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


class GroqClient:
    """Thin wrapper around Groq OpenAI-compatible API."""

    _URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key

    async def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise ValueError("GROQ_API_KEY is not set — cannot use GroqClient")
        import httpx
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": 1000,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


class LLMRouter:
    """Routes mutation requests to all available LLM clients in parallel."""

    def __init__(self, gemini_api_key: str, groq_api_key: str) -> None:
        self.clients: dict[str, LLMClient] = {
            "gemini": GeminiClient(model="gemini-2.5-flash", api_key=gemini_api_key),
            "llama": GroqClient(model="llama-3.3-70b-versatile", api_key=groq_api_key),
            "mixtral": GroqClient(model="mixtral-8x7b-32768", api_key=groq_api_key),
        }

    async def complete_all(self, system: str, user: str) -> dict[str, str]:
        """Call all clients in parallel. Skip clients that raise errors. Returns dict model→text."""

        async def _safe_complete(name: str, client: LLMClient) -> tuple[str, str | None]:
            try:
                text = await client.complete(system, user)
                return name, text
            except Exception as exc:
                logger.warning("LLM client %s failed: %s", name, exc)
                return name, None

        tasks = [_safe_complete(name, client) for name, client in self.clients.items()]
        results = await asyncio.gather(*tasks)
        return {name: text for name, text in results if text is not None}
```

- [ ] **Step 6: Запустить тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 7: Синтаксическая проверка**

```bash
cd d:\HDE_bot && python -c "
import ast
for f in ['bot/config.py', 'bot/optimizer/llm_router.py']:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 8: Commit**

```bash
git add bot/config.py bot/optimizer/__init__.py bot/optimizer/llm_router.py tests/test_prompt_optimizer.py
git commit -m "feat: optimizer LLM router — GeminiClient, GroqClient, LLMRouter"
```

---

## Task 3: Сбор данных — хук в feedback handlers

**Files:**
- Modify: `bot/topic_manager.py`
- Modify: `bot/handlers/ai_feedback.py`
- Test: `tests/test_prompt_optimizer.py`

- [ ] **Step 1: Добавить тест**

Добавить в конец `tests/test_prompt_optimizer.py`:

```python
# --- data collection ---

@pytest.mark.asyncio
async def test_implicit_feedback_saves_accepted_sample():
    """ratio >= 0.7 → сохраняется запись outcome='accepted' в optimization_samples."""
    await _db.init_db()

    # Сохранить pending feedback
    from datetime import datetime, timezone, timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO ai_feedback_pending (topic_id, ticket_id, title, history, answer_text, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (1, "T1", "Тест", "История тикета", "Нажмите кнопку обновить", expires),
        )
        await db.commit()

    # Симулируем staff reply с похожим текстом
    with patch("bot.topic_manager.db", _db):
        with patch("bot.topic_manager.index_knowledge_item", new=AsyncMock()):
            with patch("bot.topic_manager.db.delete_knowledge_item_by_ticket", new=AsyncMock(return_value=0)):
                with patch("bot.topic_manager._maybe_update_pattern", new=AsyncMock()):
                    from bot.topic_manager import _implicit_feedback
                    from unittest.mock import MagicMock
                    record = MagicMock()
                    record.topic_id = 1
                    await _implicit_feedback(record, "Нажмите кнопку обновления")

    samples = await _db.get_optimization_samples(days=1)
    assert len(samples) == 1
    assert samples[0]["outcome"] == "accepted"
    assert samples[0]["ticket_id"] == "T1"
```

- [ ] **Step 2: Запустить тест — убедиться что падает**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py::test_implicit_feedback_saves_accepted_sample -v 2>&1 | head -20
```

- [ ] **Step 3: Прочитать `_implicit_feedback` в `bot/topic_manager.py`**

Найти функцию `_implicit_feedback` (строка ~744). Понять структуру `pending` dict.

- [ ] **Step 4: Добавить `save_optimization_sample` в `_implicit_feedback`**

После блока `if ratio >= 0.7:` (после `await index_knowledge_item(...)` и логирования), добавить:

```python
        # Сохранить образец для оптимизатора промптов
        try:
            await db.save_optimization_sample(
                ticket_id=pending["ticket_id"],
                title=pending.get("title", ""),
                history=pending.get("history", ""),
                ai_answer=pending.get("answer_text", ""),
                op_answer=clean_staff,
                outcome="accepted",
                confidence=None,
            )
        except Exception as exc:
            logger.warning("save_optimization_sample failed: %s", exc)
```

После блока `elif ratio <= 0.35:` (после `await index_knowledge_item(...)` для corrected), добавить:

```python
        # Сохранить образец для оптимизатора промптов
        try:
            await db.save_optimization_sample(
                ticket_id=pending["ticket_id"],
                title=pending.get("title", ""),
                history=pending.get("history", ""),
                ai_answer=pending.get("answer_text", ""),
                op_answer=clean_staff,
                outcome="corrected",
                confidence=None,
            )
        except Exception as exc:
            logger.warning("save_optimization_sample failed: %s", exc)
```

- [ ] **Step 5: Прочитать `bot/handlers/ai_feedback.py` — найти `cb_ai_good`, `cb_ai_bad`, `cb_send_to_hde`**

Нужно понять где в каждом callback есть доступ к `pending` dict (через `get_ai_feedback_pending`).

- [ ] **Step 6: Добавить `save_optimization_sample` в `cb_ai_bad`**

Найти обработчик `F.data == "ai:bad"`. После `await delete_ai_feedback_pending(...)`, перед или после логирования, добавить:

```python
    # Сохранить для оптимизатора
    try:
        from .. import db as _db_module
        await _db_module.save_optimization_sample(
            ticket_id=pending.get("ticket_id", ""),
            title=pending.get("title", ""),
            history=pending.get("history", ""),
            ai_answer=pending.get("answer_text", ""),
            op_answer=None,
            outcome="rejected",
        )
    except Exception:
        pass
```

**Примечание:** `pending` — это результат `await get_ai_feedback_pending(...)`. Если в обработчике нет `pending`, нужно его получить перед удалением.

- [ ] **Step 7: Добавить `save_optimization_sample` в `cb_send_to_hde`**

Найти обработчик для `ai:send_post`. После успешной отправки в HDE (после `await callback.answer(...)`), добавить:

```python
    # Сохранить для оптимизатора
    try:
        from .. import db as _db_module
        await _db_module.save_optimization_sample(
            ticket_id=pending.get("ticket_id", ""),
            title=pending.get("title", ""),
            history=pending.get("history", ""),
            ai_answer=pending.get("answer_text", ""),
            op_answer=None,
            outcome="sent",
        )
    except Exception:
        pass
```

- [ ] **Step 8: Запустить тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py -v && python -m pytest tests/ -q 2>&1 | tail -5
```

- [ ] **Step 9: Синтаксическая проверка**

```bash
cd d:\HDE_bot && python -c "
import ast
for f in ['bot/topic_manager.py', 'bot/handlers/ai_feedback.py']:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 10: Commit**

```bash
git add bot/topic_manager.py bot/handlers/ai_feedback.py tests/test_prompt_optimizer.py
git commit -m "feat: optimizer data collection — save samples on feedback"
```

---

## Task 4: Mutations + Evaluator

**Files:**
- Create: `bot/optimizer/mutations.py`
- Create: `bot/optimizer/evaluator.py`
- Test: `tests/test_prompt_optimizer.py`

- [ ] **Step 1: Добавить тесты**

Добавить в конец `tests/test_prompt_optimizer.py`:

```python
# --- mutations ---

def test_build_mutation_prompt_contains_current_instructions():
    from bot.optimizer.mutations import build_mutation_prompt
    system, user = build_mutation_prompt(
        current_instructions="Ответь двумя строками.",
        good_examples=[{"ai_answer": "Хороший ответ", "op_answer": "Хороший ответ"}],
        bad_examples=[{"ai_answer": "Плохой ответ", "op_answer": None}],
    )
    assert "Ответь двумя строками." in system
    assert "Хороший ответ" in user
    assert "Плохой ответ" in user


# --- evaluator ---

@pytest.mark.asyncio
async def test_combined_score_perfect_acceptance():
    """Если все сгенерированные ответы совпадают с op_answer — score близок к 1."""
    from bot.optimizer.evaluator import combined_score

    samples = [
        {"history": "История", "title": "Тест", "ai_answer": "Ответ AI",
         "op_answer": "Ответ оператора", "outcome": "accepted", "confidence": 80},
    ]

    async def fake_generate(history, title, fmt):
        return "Ответ оператора"  # идеальное совпадение

    score = await combined_score(samples, "инструкция", _generate_fn=fake_generate)
    assert score > 0.7


@pytest.mark.asyncio
async def test_combined_score_all_rejected():
    """Все rejected — score = 0."""
    from bot.optimizer.evaluator import combined_score

    samples = [
        {"history": "История", "title": "Тест", "ai_answer": "Ответ AI",
         "op_answer": None, "outcome": "rejected", "confidence": 30},
    ]

    async def fake_generate(history, title, fmt):
        return "что-то"

    score = await combined_score(samples, "инструкция", _generate_fn=fake_generate)
    assert score == 0.0


@pytest.mark.asyncio
async def test_combined_score_empty_samples():
    """Пустой датасет — score = 0."""
    from bot.optimizer.evaluator import combined_score

    async def fake_generate(history, title, fmt):
        return "что-то"

    score = await combined_score([], "инструкция", _generate_fn=fake_generate)
    assert score == 0.0
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py::test_build_mutation_prompt_contains_current_instructions -v 2>&1 | head -10
```

- [ ] **Step 3: Создать `bot/optimizer/mutations.py`**

```python
"""Build prompts for asking LLMs to propose mutations of FORMAT_INSTRUCTIONS."""
from __future__ import annotations


_SYSTEM_TEMPLATE = """Ты эксперт по улучшению промптов для AI-ассистентов технической поддержки кассового оборудования (АТОЛ, Эвотор, Штрих-М, Viki, эквайринг).

Текущая инструкция форматирования ответов:
---
{current_instructions}
---

Твоя задача: предложить улучшенную версию этой инструкции на основе примеров работы ассистента.
Верни ТОЛЬКО новый текст инструкции, без пояснений и без markdown-форматирования."""


_USER_TEMPLATE = """Примеры где оператор принял ответ AI (хорошие):
{good_block}

Примеры где оператор отклонил или исправил ответ AI (плохие):
{bad_block}

Предложи улучшенную инструкцию форматирования."""


def build_mutation_prompt(
    current_instructions: str,
    good_examples: list[dict],
    bad_examples: list[dict],
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for a mutation request.

    Args:
        current_instructions: текущий _FORMAT_INSTRUCTIONS
        good_examples: список dict с ключами ai_answer, op_answer (outcome accepted/sent)
        bad_examples: список dict с ключами ai_answer, op_answer (outcome rejected/corrected)
    """
    good_lines = []
    for i, ex in enumerate(good_examples[:5], 1):
        good_lines.append(f"{i}. AI предложил: {ex['ai_answer'][:200]}")
        if ex.get("op_answer"):
            good_lines.append(f"   Оператор отправил: {ex['op_answer'][:200]}")

    bad_lines = []
    for i, ex in enumerate(bad_examples[:5], 1):
        bad_lines.append(f"{i}. AI предложил: {ex['ai_answer'][:200]}")
        if ex.get("op_answer"):
            bad_lines.append(f"   Оператор исправил на: {ex['op_answer'][:200]}")

    system = _SYSTEM_TEMPLATE.format(current_instructions=current_instructions)
    user = _USER_TEMPLATE.format(
        good_block="\n".join(good_lines) or "(нет примеров)",
        bad_block="\n".join(bad_lines) or "(нет примеров)",
    )
    return system, user
```

- [ ] **Step 4: Создать `bot/optimizer/evaluator.py`**

```python
"""Evaluate prompt candidates by replaying historical tickets."""
from __future__ import annotations

import difflib
import logging
import random
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_OUTCOME_WEIGHTS = {
    "sent": 1.0,
    "accepted": 0.8,
    "corrected": 0.4,
    "rejected": 0.0,
}

# Max samples to replay (limits API cost: 20 samples × 3 candidates = 60 Gemini calls)
_MAX_EVAL_SAMPLES = 20

GenerateFn = Callable[[str, str, str], Awaitable[str]]


async def combined_score(
    samples: list[dict],
    format_instructions: str,
    *,
    _generate_fn: GenerateFn | None = None,
) -> float:
    """Evaluate format_instructions on a sample set. Returns score in [0, 1].

    Args:
        samples: list of dicts from get_optimization_samples()
        format_instructions: candidate FORMAT_INSTRUCTIONS text to evaluate
        _generate_fn: injectable for testing; defaults to _generate_answer
    """
    if not samples:
        return 0.0

    generate = _generate_fn or _generate_answer

    # Limit to avoid excessive API cost
    eval_set = samples if len(samples) <= _MAX_EVAL_SAMPLES else random.sample(samples, _MAX_EVAL_SAMPLES)

    acceptance_scores: list[float] = []
    similarity_scores: list[float] = []

    for sample in eval_set:
        try:
            generated = await generate(
                sample.get("history", ""),
                sample.get("title", ""),
                format_instructions,
            )
        except Exception as exc:
            logger.warning("Evaluator generate failed for sample %s: %s", sample.get("ticket_id"), exc)
            continue

        outcome = sample.get("outcome", "rejected")
        weight = _OUTCOME_WEIGHTS.get(outcome, 0.0)

        # Acceptance: does generated answer look like what operator approved?
        op_answer = sample.get("op_answer") or sample.get("ai_answer", "")
        ratio = difflib.SequenceMatcher(
            None, generated.lower(), op_answer.lower()
        ).ratio() if op_answer else 0.0
        accepted = 1.0 if ratio >= 0.65 else 0.0
        acceptance_scores.append(accepted * weight)

        # Similarity (only for 'corrected' — op_answer is the ground truth)
        if outcome == "corrected" and sample.get("op_answer"):
            sim = difflib.SequenceMatcher(
                None, generated.lower(), sample["op_answer"].lower()
            ).ratio()
            similarity_scores.append(sim)

    if not acceptance_scores:
        return 0.0

    acceptance = sum(acceptance_scores) / len(acceptance_scores)
    similarity = sum(similarity_scores) / len(similarity_scores) if similarity_scores else 0.0
    return 0.7 * acceptance + 0.3 * similarity


async def _generate_answer(history: str, title: str, format_instructions: str) -> str:
    """Call Gemini with a custom format_instructions. Returns generated text."""
    import aiohttp
    from ..config import config

    system = (
        f"Ты AI-ассистент специалиста 2-й линии поддержки кассового оборудования.\n\n"
        f"{format_instructions}"
    )
    user = f"Тема тикета: {title}\n\n{history[-2000:]}"  # last 2000 chars

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500},
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent"
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            params={"key": config.gemini_api_key},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    # Extract only the "Ответ:" line if present
    for line in text.splitlines():
        if line.lower().startswith("ответ:"):
            return line[len("ответ:"):].strip()
    return text
```

- [ ] **Step 5: Запустить тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 6: Синтаксическая проверка**

```bash
cd d:\HDE_bot && python -c "
import ast
for f in ['bot/optimizer/mutations.py', 'bot/optimizer/evaluator.py']:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 7: Commit**

```bash
git add bot/optimizer/mutations.py bot/optimizer/evaluator.py tests/test_prompt_optimizer.py
git commit -m "feat: optimizer mutations and evaluator"
```

---

## Task 5: Agent — главный цикл

**Files:**
- Create: `bot/optimizer/agent.py`
- Test: `tests/test_prompt_optimizer.py`

- [ ] **Step 1: Добавить тесты**

Добавить в конец `tests/test_prompt_optimizer.py`:

```python
# --- agent ---

@pytest.mark.asyncio
async def test_run_optimizer_skips_when_too_few_samples():
    """< 10 сэмплов → оптимизатор не запускается."""
    await _db.init_db()
    # Только 2 сэмпла — меньше минимума
    await _db.save_optimization_sample("T1", "Тест", "История", "AI ответ", "accepted")
    await _db.save_optimization_sample("T2", "Тест2", "История2", "AI ответ2", "rejected")

    from unittest.mock import AsyncMock, MagicMock
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()

    with patch("bot.optimizer.agent.db", _db):
        with patch("bot.optimizer.agent._send_report", new=AsyncMock()):
            from bot.optimizer.agent import run_optimizer
            await run_optimizer(bot_mock)

    # Отчёт НЕ должен был отправиться (меньше минимума данных)
    from bot.optimizer.agent import _send_report
    # Если дошли сюда без ошибок — тест прошёл
    # (проверяем что bot.send_message не вызывался с отчётом)
    bot_mock.send_message.assert_not_called()
```

- [ ] **Step 2: Запустить тест — убедиться что падает**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py::test_run_optimizer_skips_when_too_few_samples -v 2>&1 | head -15
```

- [ ] **Step 3: Создать `bot/optimizer/agent.py`**

```python
"""Nightly prompt optimization agent."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import db
from ..ai_summary import _FORMAT_INSTRUCTIONS, invalidate_prompt_cache
from ..config import config
from .evaluator import combined_score
from .llm_router import LLMRouter
from .mutations import build_mutation_prompt

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 10          # минимум сэмплов для запуска
_MIN_IMPROVEMENT = 0.03    # минимальный прирост score для признания победителя


async def run_optimizer(bot: Bot) -> None:
    """Main nightly loop. Called from scheduler."""
    logger.info("Prompt optimizer: starting")

    # 1. Load dataset
    samples = await db.get_optimization_samples(days=30)
    if len(samples) < _MIN_SAMPLES:
        logger.info(
            "Prompt optimizer: not enough samples (%d < %d), skipping",
            len(samples), _MIN_SAMPLES,
        )
        return

    # 2. Get current instructions
    current_instructions = await db.get_active_prompt() or _FORMAT_INSTRUCTIONS

    # 3. Compute baseline
    try:
        baseline = await combined_score(samples, current_instructions)
    except Exception as exc:
        logger.warning("Optimizer: baseline evaluation failed: %s", exc)
        return
    logger.info("Optimizer: baseline score=%.3f on %d samples", baseline, len(samples))

    # 4. Build mutation prompts
    good = [s for s in samples if s["outcome"] in ("accepted", "sent")]
    bad = [s for s in samples if s["outcome"] in ("rejected", "corrected")]
    system_prompt, user_prompt = build_mutation_prompt(current_instructions, good, bad)

    # 5. Get mutations from all models
    router = LLMRouter(
        gemini_api_key=config.gemini_api_key,
        groq_api_key=config.groq_api_key,
    )
    try:
        mutations = await router.complete_all(system=system_prompt, user=user_prompt)
    except Exception as exc:
        logger.warning("Optimizer: mutation requests failed: %s", exc)
        return

    if not mutations:
        logger.warning("Optimizer: no mutations returned from any model")
        return

    # 6. Evaluate each mutation
    scores: dict[str, tuple[str, float]] = {}  # model → (content, score)
    for model_name, content in mutations.items():
        if not content or len(content) < 20:
            continue
        try:
            score = await combined_score(samples, content)
            scores[model_name] = (content, score)
            logger.info("Optimizer: %s score=%.3f", model_name, score)
        except Exception as exc:
            logger.warning("Optimizer: evaluation failed for %s: %s", model_name, exc)

    if not scores:
        logger.warning("Optimizer: all evaluations failed")
        return

    # 7. Find winner
    winner_model = max(scores, key=lambda m: scores[m][1])
    winner_content, winner_score = scores[winner_model]

    # 8. Save all candidates to DB
    version_ids: dict[str, int] = {}
    for model_name, (content, score) in scores.items():
        vid = await db.save_prompt_version(content=content, score=score, proposed_by=model_name)
        version_ids[model_name] = vid

    # 9. Check if winner beats baseline
    if winner_score < baseline + _MIN_IMPROVEMENT:
        logger.info(
            "Optimizer: winner %.3f does not beat baseline %.3f + threshold %.2f, no report",
            winner_score, baseline, _MIN_IMPROVEMENT,
        )
        await db.reject_all_prompt_candidates()
        return

    # 10. Send report
    winner_vid = version_ids[winner_model]
    await _send_report(
        bot=bot,
        winner_model=winner_model,
        winner_content=winner_content,
        winner_score=winner_score,
        winner_vid=winner_vid,
        baseline=baseline,
        current_instructions=current_instructions,
        sample_count=len(samples),
        all_scores=scores,
    )


async def _send_report(
    bot: Bot,
    winner_model: str,
    winner_content: str,
    winner_score: float,
    winner_vid: int,
    baseline: float,
    current_instructions: str,
    sample_count: int,
    all_scores: dict[str, tuple[str, float]],
) -> None:
    """Send Telegram report to operator with Apply/Reject/Detail buttons."""
    from ..config import config as _cfg

    improvement_pct = round((winner_score - baseline) / max(baseline, 0.01) * 100)
    baseline_int = round(baseline * 100)
    winner_int = round(winner_score * 100)

    # Diff snippet (first 150 chars)
    old_snippet = current_instructions[:150].replace("\n", " ")
    new_snippet = winner_content[:150].replace("\n", " ")

    text = (
        "🧪 <b>Ночная оптимизация промпта</b>\n\n"
        f"📊 Данные: {sample_count} тикетов · 30 дней\n"
        f"⚡ Сейчас: {baseline_int} баллов\n\n"
        f"🥇 Победитель: <b>{escape(winner_model)}</b>\n"
        f"📈 Результат: {winner_int} баллов (+{improvement_pct}%)\n\n"
        f"📝 <b>Предложенное изменение:</b>\n"
        f"— было: <i>«{escape(old_snippet)}...»</i>\n"
        f"+ стало: <i>«{escape(new_snippet)}...»</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Применить", callback_data=f"opt:apply:{winner_vid}")
    builder.button(text="❌ Отклонить", callback_data="opt:reject")
    builder.button(text="📊 Подробнее", callback_data=f"opt:detail:{winner_vid}")
    builder.adjust(2, 1)

    await bot.send_message(
        chat_id=_cfg.personal_chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    logger.info("Optimizer: report sent to operator, winner=%s score=%.3f", winner_model, winner_score)
```

- [ ] **Step 4: Добавить `invalidate_prompt_cache` в `bot/ai_summary.py`**

Пока `get_active_format_instructions` не реализована (Task 8), нужна заглушка. В `bot/ai_summary.py` добавить в конец:

```python
def invalidate_prompt_cache() -> None:
    """Invalidate cached active prompt (called after apply_prompt_version)."""
    global _active_prompt_loaded
    try:
        _active_prompt_loaded = False
    except NameError:
        pass  # cache not yet initialized
```

- [ ] **Step 5: Запустить тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py -v
```

- [ ] **Step 6: Синтаксическая проверка**

```bash
cd d:\HDE_bot && python -c "
import ast
for f in ['bot/optimizer/agent.py', 'bot/ai_summary.py']:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 7: Commit**

```bash
git add bot/optimizer/agent.py bot/ai_summary.py tests/test_prompt_optimizer.py
git commit -m "feat: optimizer agent — nightly optimization loop and report"
```

---

## Task 6: Scheduler trigger

**Files:**
- Modify: `bot/scheduler.py`

- [ ] **Step 1: Прочитать `bot/scheduler.py`**

Найти переменные `_last_*_date` в начале файла (строки ~20-26). Найти `process_scheduled_actions` — где вставить новую проверку (рядом с weekly expiry).

- [ ] **Step 2: Добавить `_last_optimization_date` и триггер**

В `bot/scheduler.py` после `_last_knowledge_expiry_date: Optional[str] = None` добавить:

```python
_last_optimization_date: Optional[str] = None
```

В `process_scheduled_actions`, после блока с weekly expiry (воскресенье 00:xx UTC), добавить:

```python
    # Ночная оптимизация промптов — ежедневно в 23:00 UTC (02:00 МСК)
    if now.hour == 23 and now.minute < 1:
        if _last_optimization_date != today:
            _last_optimization_date = today
            try:
                from .optimizer.agent import run_optimizer
                asyncio.create_task(run_optimizer(bot))
                logger.info("Scheduled prompt optimizer for tonight")
            except Exception as exc:
                logger.warning("Failed to schedule optimizer: %s", exc)
```

**Важно:** убедиться что `asyncio` уже импортирован в `scheduler.py` (он должен быть).

- [ ] **Step 3: Синтаксическая проверка**

```bash
cd d:\HDE_bot && python -c "import ast; ast.parse(open('bot/scheduler.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 4: Запустить полный тест-сьют**

```bash
cd d:\HDE_bot && python -m pytest tests/ -q 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add bot/scheduler.py
git commit -m "feat: optimizer scheduler trigger at 23:00 UTC"
```

---

## Task 7: Telegram callbacks и /aioptimize

**Files:**
- Modify: `bot/handlers/commands.py`
- Modify: `bot/main.py`
- Test: `tests/test_prompt_optimizer.py`

- [ ] **Step 1: Добавить тест для callback apply**

Добавить в конец `tests/test_prompt_optimizer.py`:

```python
# --- callbacks ---

@pytest.mark.asyncio
async def test_opt_apply_activates_version():
    """callback opt:apply:{id} → версия становится active."""
    await _db.init_db()
    vid = await _db.save_prompt_version("Новый промпт", 0.82, "llama")

    from unittest.mock import AsyncMock, MagicMock, patch
    callback = MagicMock()
    callback.data = f"opt:apply:{vid}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    with patch("bot.handlers.commands.db", _db):
        with patch("bot.handlers.commands.invalidate_prompt_cache") as mock_inv:
            from bot.handlers.commands import cb_opt_apply
            await cb_opt_apply(callback)

    active = await _db.get_active_prompt()
    assert active == "Новый промпт"
    mock_inv.assert_called_once()
```

- [ ] **Step 2: Запустить тест — убедиться что падает**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py::test_opt_apply_activates_version -v 2>&1 | head -15
```

- [ ] **Step 3: Прочитать конец `bot/handlers/commands.py`**

Найти последний хендлер + импорты в начале файла (нужны `F`, `CallbackQuery`, `router`).

- [ ] **Step 4: Добавить `/aioptimize` и callbacks в `bot/handlers/commands.py`**

В конец файла добавить:

```python
@router.message(Command("aioptimize"))
async def cmd_aioptimize(message: Message) -> None:
    """Запустить оптимизатор промптов вручную (для тестирования)."""
    from ..optimizer.agent import run_optimizer
    from ..config import config as _cfg
    if message.from_user and message.from_user.id not in _cfg.operator_telegram_user_ids:
        return
    await message.answer("🧪 Запускаю оптимизатор промптов...")
    import asyncio
    asyncio.create_task(run_optimizer(message.bot))


@router.callback_query(F.data.startswith("opt:apply:"))
async def cb_opt_apply(callback: CallbackQuery) -> None:
    from ..db import apply_prompt_version
    from ..ai_summary import invalidate_prompt_cache
    try:
        version_id = int(callback.data.split(":")[-1])
        await apply_prompt_version(version_id)
        invalidate_prompt_cache()
        await callback.answer("✅ Новый промпт применён", show_alert=True)
        try:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n<i>✅ Применено</i>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        await callback.answer(f"❌ Ошибка: {exc}", show_alert=True)


@router.callback_query(F.data == "opt:reject")
async def cb_opt_reject(callback: CallbackQuery) -> None:
    from ..db import reject_all_prompt_candidates
    await reject_all_prompt_candidates()
    await callback.answer("❌ Отклонено", show_alert=False)
    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n<i>❌ Отклонено</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("opt:detail:"))
async def cb_opt_detail(callback: CallbackQuery) -> None:
    from ..db import get_optimization_samples
    await callback.answer()
    try:
        version_id = int(callback.data.split(":")[-1])
        samples = await get_optimization_samples(days=30)
        total = len(samples)
        by_outcome: dict[str, int] = {}
        for s in samples:
            by_outcome[s["outcome"]] = by_outcome.get(s["outcome"], 0) + 1

        lines = [
            f"📊 <b>Детали оптимизации</b>",
            f"Всего сэмплов: {total}",
        ]
        for outcome, count in sorted(by_outcome.items()):
            lines.append(f"  • {outcome}: {count}")

        await callback.message.answer(
            "\n".join(lines),
            parse_mode="HTML",
        )
    except Exception as exc:
        await callback.message.answer(f"❌ Ошибка: {exc}")
```

- [ ] **Step 5: Добавить BotCommand в `bot/main.py`**

Найти список `BotCommand` (рядом с `aimetrics`). Добавить:

```python
        BotCommand(command="aioptimize", description="Запустить оптимизацию промпта вручную"),
```

- [ ] **Step 6: Запустить тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py -v && python -m pytest tests/ -q 2>&1 | tail -5
```

- [ ] **Step 7: Синтаксическая проверка**

```bash
cd d:\HDE_bot && python -c "
import ast
for f in ['bot/handlers/commands.py', 'bot/main.py']:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 8: Commit**

```bash
git add bot/handlers/commands.py bot/main.py tests/test_prompt_optimizer.py
git commit -m "feat: /aioptimize command and opt:apply/reject/detail callbacks"
```

---

## Task 8: Lazy prompt loading в ai_summary.py

**Files:**
- Modify: `bot/ai_summary.py`
- Test: `tests/test_prompt_optimizer.py`

- [ ] **Step 1: Добавить тест**

Добавить в конец `tests/test_prompt_optimizer.py`:

```python
# --- lazy prompt loading ---

@pytest.mark.asyncio
async def test_get_active_format_instructions_returns_builtin_when_no_db():
    """Если нет active версии в БД — возвращает встроенный _FORMAT_INSTRUCTIONS."""
    await _db.init_db()
    from bot.ai_summary import get_active_format_instructions, _FORMAT_INSTRUCTIONS
    import bot.ai_summary as _ai_mod
    # Сбросить кэш
    _ai_mod._active_prompt_loaded = False
    result = await get_active_format_instructions()
    assert result == _FORMAT_INSTRUCTIONS


@pytest.mark.asyncio
async def test_get_active_format_instructions_returns_db_version():
    """Если есть active версия в БД — возвращает её."""
    await _db.init_db()
    vid = await _db.save_prompt_version("Кастомная инструкция", 0.85, "llama")
    await _db.apply_prompt_version(vid)

    import bot.ai_summary as _ai_mod
    _ai_mod._active_prompt_loaded = False  # сбросить кэш

    from bot.ai_summary import get_active_format_instructions
    result = await get_active_format_instructions()
    assert result == "Кастомная инструкция"
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py::test_get_active_format_instructions_returns_builtin_when_no_db -v 2>&1 | head -15
```

- [ ] **Step 3: Прочитать `bot/ai_summary.py` — найти `_FORMAT_INSTRUCTIONS` и `_build_system_prompt`**

Нужно понять где `_FORMAT_INSTRUCTIONS` используется в `_build_system_prompt` и в `generate_ticket_summary`.

- [ ] **Step 4: Добавить кэш и функцию в `bot/ai_summary.py`**

После `_FORMAT_INSTRUCTIONS` (после строки ~70), добавить:

```python
_active_prompt_loaded: bool = False
_active_format_instructions: str | None = None


async def get_active_format_instructions() -> str:
    """Return active FORMAT_INSTRUCTIONS from DB (lazily loaded, cached in memory).

    Falls back to built-in _FORMAT_INSTRUCTIONS if nothing in DB.
    Call invalidate_prompt_cache() after applying a new version.
    """
    global _active_prompt_loaded, _active_format_instructions
    if not _active_prompt_loaded:
        try:
            from . import db as _db_module
            _active_format_instructions = await _db_module.get_active_prompt()
        except Exception:
            _active_format_instructions = None
        _active_prompt_loaded = True
    return _active_format_instructions or _FORMAT_INSTRUCTIONS


def invalidate_prompt_cache() -> None:
    """Invalidate cached active prompt (call after apply_prompt_version)."""
    global _active_prompt_loaded
    _active_prompt_loaded = False
```

- [ ] **Step 5: Обновить `generate_ticket_summary` для использования активного промпта**

Найти в `generate_ticket_summary` место где вызывается `_build_system_prompt`. Перед этим вызовом добавить:

```python
    # Use active prompt version if available
    format_instructions = await get_active_format_instructions()
```

Изменить `_build_system_prompt` чтобы принимал `format_instructions` параметр:

```python
def _build_system_prompt(
    ticket_title: str,
    rag_examples: list[str] | None = None,
    wiki_context: str | None = None,
    equipment: str | None = None,
    solution_steps: str | None = None,
    format_instructions: str | None = None,
) -> str:
    instr = format_instructions or _FORMAT_INSTRUCTIONS
    # ... остальной код использует instr вместо _FORMAT_INSTRUCTIONS
```

Передать `format_instructions=format_instructions` при вызове `_build_system_prompt(...)` в `generate_ticket_summary`.

**Внимание:** перед редактированием прочитать полностью `_build_system_prompt` и весь `generate_ticket_summary`, чтобы точно знать где и что менять.

- [ ] **Step 6: Удалить заглушку `invalidate_prompt_cache` из Task 5**

В Task 5 был добавлен stub `invalidate_prompt_cache` в конец `ai_summary.py`. Теперь реальная функция определена выше — удалить дубликат. Проверить:

```bash
grep -n "def invalidate_prompt_cache" d:\HDE_bot\bot\ai_summary.py
```

Если два определения — удалить нижнее (заглушку).

- [ ] **Step 7: Запустить все тесты**

```bash
cd d:\HDE_bot && python -m pytest tests/test_prompt_optimizer.py -v && python -m pytest tests/ -q 2>&1 | tail -5
```

Ожидаем: все test_prompt_optimizer PASSED, нет регрессий.

- [ ] **Step 8: Синтаксическая проверка всех изменённых файлов**

```bash
cd d:\HDE_bot && python -c "
import ast
files = [
    'bot/db.py',
    'bot/config.py',
    'bot/optimizer/__init__.py',
    'bot/optimizer/llm_router.py',
    'bot/optimizer/mutations.py',
    'bot/optimizer/evaluator.py',
    'bot/optimizer/agent.py',
    'bot/ai_summary.py',
    'bot/topic_manager.py',
    'bot/handlers/ai_feedback.py',
    'bot/scheduler.py',
    'bot/handlers/commands.py',
    'bot/main.py',
]
for f in files:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 9: Финальный commit**

```bash
git add bot/ai_summary.py tests/test_prompt_optimizer.py
git commit -m "feat: lazy prompt loading in ai_summary from prompt_versions DB"
```

- [ ] **Step 10: Git push**

```bash
git push origin main
```

---

## Финальная проверка

- [ ] Полный тест-сьют:

```bash
cd d:\HDE_bot && python -m pytest tests/ -v 2>&1 | tail -10
```

- [ ] Добавить `GROQ_API_KEY=your_key_here` в `.env.example`

- [ ] На VPS:

```bash
git pull && systemctl restart hde-bot
```

- [ ] После нескольких тикетов с фидбеком: `/aioptimize` → должен прийти отчёт или сообщение "недостаточно данных"

---

## Что делать вручную после деплоя

1. Получить Groq API key на [console.groq.com](https://console.groq.com) (бесплатно)
2. Добавить `GROQ_API_KEY=your_key` в `.env` на VPS
3. `systemctl restart hde-bot`
4. Накопить 10+ тикетов с фидбеком (👍/✏️/👎)
5. Нажать `/aioptimize` — первый ручной запуск
6. Оценить предложение в отчёте, нажать ✅ или ❌
7. Дальше — каждую ночь автоматически в 02:00 МСК
