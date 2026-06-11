# AI Answer Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Повысить точность AI-подсказок для специалиста 2-й линии поддержки кассового оборудования через специализированный промпт, детекцию бренда, базу паттернов решений и company-фильтрацию RAG.

**Architecture:** Восемь компонентов: новая таблица `solution_patterns` в SQLite, детекция оборудования по regex, обновлённый system prompt с ролью специалиста 2й линии, company-буст в RRF, confidence score в UI, команда `/aianalyze`, непрерывное усиление паттернов через implicit feedback.

**Tech Stack:** Python 3.10+, aiosqlite, aiohttp, Gemini API (gemini-2.5-flash), aiogram 3.x, difflib (stdlib), re (stdlib)

---

## File Map

| Файл | Изменения |
|---|---|
| `bot/db.py` | +таблица `solution_patterns`, +5 CRUD функций, `list_all_knowledge_embeddings` возвращает `company_id` |
| `bot/ai_summary.py` | +`_EQUIPMENT_PATTERNS`, +`_detect_equipment()`, обновить `_FORMAT_INSTRUCTIONS`, обновить `_build_system_prompt()` сигнатуру, обновить `generate_ticket_summary()` |
| `bot/knowledge/store.py` | `find_similar()` +`company_id` параметр, company-буст в RRF, возвращать `list[tuple[KnowledgeItem, float]]` |
| `bot/knowledge/indexer.py` | `get_rag_context()` +`company_id`, возвращать `tuple[list[str], int]` |
| `bot/topic_manager.py` | unpack confidence_pct, рендер `🧠 Суть (78%):`, +`_maybe_update_pattern()` |
| `bot/handlers/commands.py` | +`cmd_aianalyze()` |
| `bot/main.py` | +BotCommand `aianalyze` |
| `tests/test_ai_answer_quality.py` | новый файл тестов |

---

## Task 1: solution_patterns DB schema + CRUD

**Files:**
- Modify: `bot/db.py`
- Test: `tests/test_ai_answer_quality.py`

- [ ] **Step 1: Написать тесты для CRUD**

```python
# tests/test_ai_answer_quality.py
import asyncio
import pytest
import aiosqlite
from bot import db as _db

@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))

@pytest.mark.asyncio
async def test_save_and_find_pattern():
    await _db.init_db()
    pid = await _db.save_solution_pattern(
        problem_type="ошибка ОФД",
        steps="1. Меню ФН → 2. Диагностика ОФД",
        source="analyze",
        equipment="АТОЛ",
    )
    assert pid > 0
    result = await _db.find_solution_pattern("АТОЛ", "ошибка ОФД не отвечает")
    assert result is not None
    assert result["equipment"] == "АТОЛ"
    assert "ОФД" in result["steps"]

@pytest.mark.asyncio
async def test_increment_pattern_use():
    await _db.init_db()
    pid = await _db.save_solution_pattern("замена ФН", "Закрыть смену → Заменить ФН", "analyze")
    await _db.increment_pattern_use(pid)
    await _db.increment_pattern_use(pid)
    patterns = await _db.list_solution_patterns()
    assert patterns[0]["use_count"] == 2

@pytest.mark.asyncio
async def test_pattern_exists_similar_detects_duplicate():
    await _db.init_db()
    await _db.save_solution_pattern("ошибка соединения ОФД", "Шаг 1", "analyze", "АТОЛ")
    assert await _db.pattern_exists_similar("АТОЛ", "ошибка подключения ОФД") is True
    assert await _db.pattern_exists_similar("АТОЛ", "замена ФН регистратора") is False

@pytest.mark.asyncio
async def test_count_patterns_by_equipment():
    await _db.init_db()
    await _db.save_solution_pattern("ошибка ОФД", "Шаги", "analyze", "АТОЛ")
    await _db.save_solution_pattern("не печатает", "Шаги", "analyze", "Эвотор")
    await _db.save_solution_pattern("сеть", "Шаги", "analyze", None)
    counts = await _db.count_solution_patterns_by_equipment()
    assert counts.get("АТОЛ") == 1
    assert counts.get("Эвотор") == 1
    assert counts.get("Без бренда") == 1
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```
pytest tests/test_ai_answer_quality.py -v
```
Ожидаем: ImportError или AttributeError — функции ещё не существуют.

- [ ] **Step 3: Добавить таблицу `solution_patterns` в `init_db` (`bot/db.py`)**

Найти блок после `ai_feedback_pending` (строка ~284) и добавить:

```python
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS solution_patterns (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                equipment    TEXT,
                problem_type TEXT NOT NULL,
                steps        TEXT NOT NULL,
                source       TEXT NOT NULL DEFAULT 'analyze',
                use_count    INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solution_patterns_equip
            ON solution_patterns(equipment, problem_type)
            """
        )
```

- [ ] **Step 4: Добавить CRUD функции в конец `bot/db.py`**

```python
async def save_solution_pattern(
    problem_type: str,
    steps: str,
    source: str = "analyze",
    equipment: Optional[str] = None,
) -> int:
    """Insert a new solution pattern. Returns new row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO solution_patterns (equipment, problem_type, steps, source) VALUES (?, ?, ?, ?)",
            (equipment, problem_type, steps, source),
        )
        await db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid


async def find_solution_pattern(
    equipment: Optional[str],
    keywords: str,
) -> Optional[dict]:
    """Return the best matching pattern for given equipment and keywords, or None."""
    import re as _re
    words = {w for w in _re.sub(r"[^\w\s]", " ", keywords.lower()).split() if len(w) >= 3}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Prefer equipment-specific patterns; fall back to universal (NULL)
        candidates: list[aiosqlite.Row] = []
        if equipment:
            async with db.execute(
                "SELECT * FROM solution_patterns WHERE equipment = ? ORDER BY use_count DESC LIMIT 10",
                (equipment,),
            ) as cur:
                candidates = list(await cur.fetchall())
        if not candidates:
            async with db.execute(
                "SELECT * FROM solution_patterns WHERE equipment IS NULL ORDER BY use_count DESC LIMIT 10",
            ) as cur:
                candidates = list(await cur.fetchall())
        if not candidates:
            return None
        # Score by keyword overlap with problem_type
        best: Optional[dict] = None
        best_score = -1
        for row in candidates:
            row_words = set(row["problem_type"].lower().split())
            score = len(words & row_words)
            if score > best_score:
                best_score = score
                best = dict(row)
        return best  # may be row[0] if no keyword overlap (score 0)


async def increment_pattern_use(pattern_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE solution_patterns SET use_count = use_count + 1 WHERE id = ?",
            (pattern_id,),
        )
        await db.commit()


async def list_solution_patterns(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM solution_patterns ORDER BY use_count DESC, created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def pattern_exists_similar(
    equipment: Optional[str],
    problem_type: str,
) -> bool:
    """Return True if a pattern with same equipment and similar problem_type exists."""
    import difflib
    patterns = await list_solution_patterns(limit=200)
    for p in patterns:
        if p["equipment"] != equipment:
            continue
        ratio = difflib.SequenceMatcher(
            None, p["problem_type"].lower(), problem_type.lower()
        ).ratio()
        if ratio >= 0.7:
            return True
    return False


async def count_solution_patterns_by_equipment() -> dict[str, int]:
    """Return {equipment_label: count} for reporting."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(equipment, 'Без бренда'), COUNT(*) "
            "FROM solution_patterns GROUP BY equipment ORDER BY COUNT(*) DESC"
        ) as cur:
            return dict(await cur.fetchall())
```

- [ ] **Step 5: Запустить тесты — убедиться что проходят**

```
pytest tests/test_ai_answer_quality.py -v
```
Ожидаем: 4 PASSED.

- [ ] **Step 6: Commit**

```bash
git add bot/db.py tests/test_ai_answer_quality.py
git commit -m "feat: add solution_patterns table and CRUD to db.py"
```

---

## Task 2: Equipment detection + обновить _FORMAT_INSTRUCTIONS и _build_system_prompt

**Files:**
- Modify: `bot/ai_summary.py`
- Test: `tests/test_ai_answer_quality.py`

- [ ] **Step 1: Добавить тесты**

```python
# Добавить в tests/test_ai_answer_quality.py
from bot.ai_summary import _detect_equipment, _build_system_prompt

def test_detect_equipment_atol():
    assert _detect_equipment("АТОЛ 30Ф ошибка ОФД", "") == "АТОЛ"

def test_detect_equipment_in_history():
    assert _detect_equipment("Проблема с кассой", "У нас стоит Эвотор 7.2") == "Эвотор"

def test_detect_equipment_none():
    assert _detect_equipment("Не могу войти в личный кабинет", "нет связи") is None

def test_detect_equipment_acquiring():
    assert _detect_equipment("Терминал Сбера не работает", "") == "Эквайринг Сбер"

def test_build_system_prompt_contains_role():
    text = _build_system_prompt("тест")
    assert "2-й линии" in text
    assert "СПЕЦИАЛИСТУ" in text

def test_build_system_prompt_with_equipment():
    text = _build_system_prompt("тест", equipment="АТОЛ")
    assert "АТОЛ" in text

def test_build_system_prompt_with_steps():
    text = _build_system_prompt("тест", solution_steps="1. Меню ФН → 2. Диагностика")
    assert "Типовые шаги" in text
    assert "Меню ФН" in text
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```
pytest tests/test_ai_answer_quality.py::test_detect_equipment_atol -v
```
Ожидаем: ImportError — `_detect_equipment` не существует.

- [ ] **Step 3: Добавить `_EQUIPMENT_PATTERNS` и `_detect_equipment` в `bot/ai_summary.py`**

Вставить после блока `_MAX_IMAGE_BYTES = 4 * 1024 * 1024`:

```python
_EQUIPMENT_PATTERNS = [
    (r'\bатол\b|atol|\bфр\b', 'АТОЛ'),
    (r'\bэвотор\b|evotor', 'Эвотор'),
    (r'\bштрих\b|shtrih|shtrikh', 'Штрих-М'),
    (r'\bviki\b|вики', 'Viki'),
    (r'\bсбербанк\b|\bсбер\b|sber', 'Эквайринг Сбер'),
    (r'\bвтб\b|vtb', 'Эквайринг ВТБ'),
    (r'\bтинькофф\b|tinkoff|тиньков', 'Эквайринг Тинькофф'),
    (r'\bптк\b|ptkf', 'ПТК'),
]


def _detect_equipment(title: str, history: str) -> str | None:
    """Return equipment brand from ticket title + start of history, or None."""
    import re
    text = (title + " " + history[:300]).lower()
    for pattern, brand in _EQUIPMENT_PATTERNS:
        if re.search(pattern, text):
            return brand
    return None
```

- [ ] **Step 4: Заменить `_FORMAT_INSTRUCTIONS` и `_build_system_prompt` в `bot/ai_summary.py`**

Заменить текущий `_FORMAT_INSTRUCTIONS` (строки 41-49):

```python
_FORMAT_INSTRUCTIONS = (
    "Ответь РОВНО двумя строками — обе обязательны:\n"
    "Суть: <диагноз проблемы, бренд/модель если известны>\n"
    "Ответ: <конкретные технические шаги через →>\n\n"
    "Пример:\n"
    "Суть: АТОЛ 30Ф — ошибка связи с ОФД, истёк сертификат.\n"
    "Ответ: Меню ФН → Диагностика ОФД → Обновить сертификат в ЛК ОФД → Перерегистрация.\n\n"
    "ВАЖНО: шаги для специалиста, не для клиента. Не используй markdown. Не добавляй ничего лишнего."
)
```

Заменить функцию `_build_system_prompt` (текущая сигнатура: `ticket_title, rag_examples, wiki_context`):

```python
def _build_system_prompt(
    ticket_title: str,
    rag_examples: list[str] | None = None,
    wiki_context: str | None = None,
    equipment: str | None = None,
    solution_steps: str | None = None,
) -> str:
    base = (
        "Ты — помощник технического специалиста 2-й линии поддержки.\n"
        "Специализация: кассовое оборудование (АТОЛ, Эвотор, Штрих-М, Viki),\n"
        "фискальные регистраторы, ОФД/ФН, сетевые подключения, эквайринг\n"
        "(Сбер, ВТБ, Тинькофф).\n\n"
        "Тикет передан с 1-й линии — базовую диагностику уже провели.\n\n"
        "ВАЖНО: ты подсказываешь СПЕЦИАЛИСТУ что делать, не пишешь ответ клиенту.\n"
        "Ответ — техническая инструкция к выполнению.\n\n"
    )
    if ticket_title:
        base += f"Тема обращения: «{ticket_title}»\n\n"
    if equipment:
        base += f"Оборудование в тикете: {equipment}\n\n"
    if solution_steps:
        base += (
            "Типовые шаги решения для этого типа проблемы:\n"
            f"{solution_steps}\n\n"
            "---\n\n"
        )
    if wiki_context:
        base += (
            "Справочная статья из базы знаний:\n\n"
            f"{wiki_context}\n\n"
            "---\n\n"
        )
    if rag_examples:
        examples_text = "\n\n---\n\n".join(rag_examples)
        base += (
            "Примеры решений из практики:\n\n"
            f"{examples_text}\n\n"
            "---\n\n"
            "Теперь обработай новый тикет:\n\n"
        )
    return base + _FORMAT_INSTRUCTIONS
```

- [ ] **Step 5: Запустить тесты**

```
pytest tests/test_ai_answer_quality.py -v
```
Ожидаем: все тесты PASSED.

- [ ] **Step 6: Commit**

```bash
git add bot/ai_summary.py tests/test_ai_answer_quality.py
git commit -m "feat: equipment detection + specialized 2nd-line tech support prompt"
```

---

## Task 3: find_similar — company boost + confidence score

**Files:**
- Modify: `bot/db.py` (обновить `list_all_knowledge_embeddings`)
- Modify: `bot/knowledge/store.py`
- Test: `tests/test_ai_answer_quality.py`

- [ ] **Step 1: Добавить тест**

```python
# Добавить в tests/test_ai_answer_quality.py
import numpy as np
from bot.knowledge.store import find_similar, cosine_similarity
from bot import db as _db

@pytest.mark.asyncio
async def test_find_similar_returns_confidence():
    await _db.init_db()
    # Создать фиктивный knowledge item
    emb = np.ones(4, dtype=np.float32)
    emb_bytes = emb.tobytes()
    await _db.save_knowledge_item(
        source="test", content="АТОЛ ошибка ОФД — обновить сертификат",
        embedding=emb_bytes, quality="good", company_id="corp1",
    )
    results = await find_similar(emb, limit=1, query_text="", company_id="corp1")
    assert len(results) == 1
    item, score = results[0]
    assert 0.0 <= score <= 1.0
    assert "АТОЛ" in item.content

@pytest.mark.asyncio
async def test_find_similar_company_boost():
    await _db.init_db()
    emb = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    same_co = np.array([0.9, 0.1, 0.0, 0.0], dtype=np.float32)
    diff_co = np.array([0.8, 0.0, 0.6, 0.0], dtype=np.float32)
    # Нормализуем
    same_co = same_co / np.linalg.norm(same_co)
    diff_co = diff_co / np.linalg.norm(diff_co)
    await _db.save_knowledge_item(
        source="test", content="same company item", embedding=same_co.tobytes(),
        quality="good", company_id="corp1",
    )
    await _db.save_knowledge_item(
        source="test", content="diff company item", embedding=diff_co.tobytes(),
        quality="good", company_id="corp2",
    )
    results = await find_similar(emb, limit=2, company_id="corp1")
    # same company item должен быть первым благодаря бусту
    assert results[0][0].content == "same company item"
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```
pytest tests/test_ai_answer_quality.py::test_find_similar_returns_confidence -v
```
Ожидаем: AssertionError — `find_similar` возвращает `list[KnowledgeItem]`, не `list[tuple]`.

- [ ] **Step 3: Обновить `list_all_knowledge_embeddings` в `bot/db.py`**

Найти функцию `list_all_knowledge_embeddings` и изменить:

```python
async def list_all_knowledge_embeddings() -> list[tuple[int, str, bytes, str]]:
    """Return (id, content, embedding, company_id) for all indexed items."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, content, embedding, COALESCE(company_id, '') FROM knowledge_items "
            "WHERE embedding IS NOT NULL AND quality != 'bad'"
        ) as cur:
            return await cur.fetchall()
```

- [ ] **Step 4: Обновить `find_similar` в `bot/knowledge/store.py`**

Полностью заменить функцию `find_similar`:

```python
async def find_similar(
    query_embedding: np.ndarray,
    *,
    limit: int = 3,
    query_text: str = "",
    company_id: str = "",
) -> list[tuple[KnowledgeItem, float]]:
    """Return top-N (item, cosine_score) tuples.

    When query_text provided: merges cosine + BM25 via RRF.
    When company_id provided: same-company items get an extra RRF boost.
    """
    rows = await list_all_knowledge_embeddings()
    if not rows:
        return []

    # Cosine scoring
    cosine_scored: list[tuple[float, int, str, str]] = []  # (score, id, content, item_company_id)
    for row_id, content, emb_bytes, item_company_id in rows:
        try:
            emb = bytes_to_embedding(emb_bytes)
        except Exception:
            logger.warning("Skipping corrupted embedding row_id=%s", row_id, exc_info=True)
            continue
        score = cosine_similarity(query_embedding, emb)
        cosine_scored.append((score, row_id, content, item_company_id))
    cosine_scored.sort(key=lambda x: x[0], reverse=True)

    k = 60  # RRF constant
    rrf_scores: dict[int, float] = {}
    contents: dict[int, str] = {}
    cosine_top: dict[int, float] = {}  # for confidence score

    pool = cosine_scored[: limit * 3]

    for rank, (score, item_id, content, _) in enumerate(pool):
        rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
        contents[item_id] = content
        cosine_top[item_id] = score

    if query_text:
        bm25_rows = await fts_search_knowledge(query_text, limit=limit * 3)
        for rank, (item_id, content) in enumerate(bm25_rows):
            rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
            contents[item_id] = content

    # Company boost: same-company items get bonus equivalent to rank-1 position
    if company_id:
        for _, item_id, _, item_company_id in pool:
            if item_company_id == company_id and item_id in rrf_scores:
                rrf_scores[item_id] += 1.0 / (k + 1)

    sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)
    result = []
    for item_id in sorted_ids[:limit]:
        score = cosine_top.get(item_id, 0.0)
        result.append((
            KnowledgeItem(id=item_id, source="", ticket_id=None, title=None,
                          content=contents[item_id], quality="good"),
            score,
        ))
    return result
```

- [ ] **Step 5: Запустить тесты**

```
pytest tests/test_ai_answer_quality.py -v
```
Ожидаем: все тесты PASSED.

- [ ] **Step 6: Commit**

```bash
git add bot/db.py bot/knowledge/store.py tests/test_ai_answer_quality.py
git commit -m "feat: find_similar returns (item, confidence) tuples + company RRF boost"
```

---

## Task 4: get_rag_context возвращает confidence

**Files:**
- Modify: `bot/knowledge/indexer.py`
- Test: `tests/test_ai_answer_quality.py`

- [ ] **Step 1: Добавить тест**

```python
# Добавить в tests/test_ai_answer_quality.py
from bot.knowledge.indexer import get_rag_context

@pytest.mark.asyncio
async def test_get_rag_context_returns_confidence():
    await _db.init_db()
    examples, confidence = await get_rag_context("тест", "история", company_id="")
    assert isinstance(examples, list)
    assert isinstance(confidence, int)
    assert 0 <= confidence <= 100
```

- [ ] **Step 2: Запустить тест — убедиться что падает**

```
pytest tests/test_ai_answer_quality.py::test_get_rag_context_returns_confidence -v
```
Ожидаем: AssertionError — `get_rag_context` возвращает `list[str]`.

- [ ] **Step 3: Обновить `get_rag_context` в `bot/knowledge/indexer.py`**

Заменить полностью:

```python
async def get_rag_context(
    ticket_title: str,
    history_tail: str,
    *,
    limit: int = 3,
    company_id: str = "",
) -> tuple[list[str], int]:
    """Return (content_list, max_confidence_pct) for top-N similar knowledge items."""
    query = f"{ticket_title}\n{history_tail[-600:]}"
    embedding = await embed_text(query, task_type="query")
    if embedding is None:
        return [], 0
    similar = await find_similar(
        embedding, limit=limit, query_text=query, company_id=company_id
    )
    if not similar:
        return [], 0
    examples = [item.content for item, _ in similar]
    max_score = max(score for _, score in similar)
    confidence_pct = int(max_score * 100)
    return examples, confidence_pct
```

- [ ] **Step 4: Запустить тесты**

```
pytest tests/test_ai_answer_quality.py -v
```
Ожидаем: все PASSED.

- [ ] **Step 5: Commit**

```bash
git add bot/knowledge/indexer.py tests/test_ai_answer_quality.py
git commit -m "feat: get_rag_context returns (examples, confidence_pct) tuple"
```

---

## Task 5: Собрать всё в generate_ticket_summary

**Files:**
- Modify: `bot/ai_summary.py`

- [ ] **Step 1: Обновить сигнатуру `generate_ticket_summary`**

Изменить сигнатуру функции (строка ~227):

```python
async def generate_ticket_summary(
    posts: "list[HDEPost]",
    info: "HDETicketInfo",
    ticket_title: str = "",
    ticket_id: str = "",
    company_id: str = "",
) -> tuple[str, str, int] | None:
    """Return (suit_line, answer_line, confidence_pct) or None if disabled/failed."""
```

- [ ] **Step 2: Обновить тело функции — вставить equipment, pattern, confidence**

Найти блок RAG (~строка 253-271) и заменить всё от `# RAG:` до `system_text = _build_system_prompt(...)`:

```python
    # Equipment detection
    equipment = _detect_equipment(ticket_title, history)

    # RAG: find similar examples from knowledge base
    rag_examples: list[str] = []
    confidence_pct: int = 0
    try:
        from .knowledge.indexer import get_rag_context
        rag_examples, confidence_pct = await get_rag_context(
            ticket_title, history, company_id=company_id
        )
    except Exception as exc:
        logger.warning("RAG context retrieval failed: %s", exc)

    # Solution pattern lookup
    solution_steps: str | None = None
    try:
        from . import db as _db2
        pattern = await _db2.find_solution_pattern(equipment, ticket_title)
        if pattern:
            solution_steps = pattern["steps"]
            await _db2.increment_pattern_use(pattern["id"])
            logger.debug("Pattern hit for ticket %s: %r", ticket_id, solution_steps[:60])
    except Exception as exc:
        logger.warning("Solution pattern lookup failed: %s", exc)

    # Wiki: find relevant article for this topic
    wiki_ctx: str | None = None
    try:
        from .wiki.searcher import get_wiki_context
        wiki_ctx = await get_wiki_context(ticket_title)
        if wiki_ctx:
            logger.info("Wiki context found for ticket %s (%d chars)", ticket_id, len(wiki_ctx))
    except Exception as exc:
        logger.warning("Wiki context retrieval failed: %s", exc)

    system_text = _build_system_prompt(
        ticket_title,
        rag_examples or None,
        wiki_ctx,
        equipment=equipment,
        solution_steps=solution_steps,
    )
```

- [ ] **Step 3: Обновить return в конце функции**

Найти `return (suit_line, answer_line)` и заменить:

```python
    return (suit_line, answer_line, confidence_pct)
```

- [ ] **Step 4: Проверить синтаксис**

```bash
python -c "import ast; ast.parse(open('bot/ai_summary.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add bot/ai_summary.py
git commit -m "feat: wire equipment detection, pattern injection, confidence into generate_ticket_summary"
```

---

## Task 6: Рендер confidence в topic_manager.py

**Files:**
- Modify: `bot/topic_manager.py`

- [ ] **Step 1: Найти место вызова generate_ticket_summary и обновить unpack**

Найти строку `suit_line, answer_line = result` (~строка 307) и заменить:

```python
        suit_line, answer_line, confidence_pct = result
```

- [ ] **Step 2: Обновить рендер сообщения «Суть»**

Найти строку с `f"🧠 <b>Суть:</b> {_html_escape(suit_line)}"` и заменить:

```python
                suit_label = (
                    f"🧠 <b>Суть ({confidence_pct}%):</b>"
                    if confidence_pct >= 40
                    else "🧠 <b>Суть:</b>"
                )
                await bot.send_message(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"{suit_label} {_html_escape(suit_line)}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=suit_feedback_kb(),
                )
```

- [ ] **Step 3: Обновить вызов generate_ticket_summary чтобы передавать company_id**

Найти вызов `result = await generate_ticket_summary(...)` и добавить `company_id`:

```python
    result = await generate_ticket_summary(
        posts, info,
        ticket_title=ticket_title,
        ticket_id=ticket_id,
        company_id=_payload_value(payload, "company_id") or "",
    )
```

- [ ] **Step 4: Проверить синтаксис**

```bash
python -c "import ast; ast.parse(open('bot/topic_manager.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add bot/topic_manager.py
git commit -m "feat: render confidence score in Суть message, pass company_id to RAG"
```

---

## Task 7: Команда /aianalyze

**Files:**
- Modify: `bot/handlers/commands.py`
- Modify: `bot/main.py`

- [ ] **Step 1: Добавить `cmd_aianalyze` в конец `bot/handlers/commands.py`**

```python
@router.message(Command("aianalyze"))
async def cmd_aianalyze(message: Message) -> None:
    """Analyze knowledge_items and populate solution_patterns table."""
    from ..db import (
        list_solution_patterns,
        save_solution_pattern,
        pattern_exists_similar,
        count_solution_patterns_by_equipment,
    )
    from ..config import config as _config
    import json as _json
    import time as _time

    if not _config.gemini_api_key:
        await message.answer("❌ GEMINI_API_KEY не настроен")
        return

    # Load all suitable knowledge items
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT title, content FROM knowledge_items "
            "WHERE source IN ('hde_closed', 'feedback', 'implicit_good') "
            "AND quality != 'bad' AND content != '' "
            "ORDER BY created_at DESC LIMIT 2000"
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await message.answer("ℹ️ Нет тикетов для анализа. Сначала запусти /aiimport")
        return

    status_msg = await message.answer(f"⏳ Начинаю анализ {len(items)} тикетов...")
    t_start = _time.monotonic()
    batch_size = 15
    created = 0
    skipped = 0

    _ANALYZE_PROMPT = (
        "Ты анализируешь решённые тикеты технической поддержки кассового оборудования.\n"
        "Из каждого тикета извлеки:\n"
        "- equipment: бренд оборудования (АТОЛ/Эвотор/Штрих-М/Viki/Эквайринг Сбер/ВТБ/Тинькофф/ПТК) "
        "или null если не определён\n"
        "- problem_type: краткое описание типа проблемы (5-10 слов)\n"
        "- steps: конкретные шаги решения через → (если шагов нет — пропусти тикет)\n\n"
        "Верни JSON-массив: [{\"equipment\": ..., \"problem_type\": ..., \"steps\": ...}, ...]\n"
        "Включай только тикеты с явными шагами решения. Пропускай общие вопросы без решения.\n"
        "Только JSON, без markdown.\n\nТикеты:\n"
    )

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            batch_text = ""
            for idx, (title, content) in enumerate(batch, 1):
                batch_text += f"\n[{idx}] {title}\n{content[:400]}\n"

            payload = {
                "contents": [{"parts": [{"text": _ANALYZE_PROMPT + batch_text}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2000},
            }
            try:
                async with session.post(
                    (
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        "gemini-2.5-flash:generateContent"
                    ),
                    json=payload,
                    params={"key": _config.gemini_api_key},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        skipped += len(batch)
                        continue
                    data = await resp.json()
                    raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    # Strip markdown code fences if present
                    import re as _re
                    raw = _re.sub(r"^```[^\n]*\n?", "", raw).rstrip("`").strip()
                    patterns = _json.loads(raw)
            except Exception as exc:
                logger.warning("aianalyze batch %d failed: %s", i, exc)
                skipped += len(batch)
                continue

            for p in patterns:
                eq = p.get("equipment") or None
                pt = (p.get("problem_type") or "").strip()
                st = (p.get("steps") or "").strip()
                if not pt or not st:
                    continue
                if await pattern_exists_similar(eq, pt):
                    skipped += 1
                    continue
                await save_solution_pattern(
                    problem_type=pt, steps=st, source="analyze", equipment=eq
                )
                created += 1

            # Update progress every 50 items
            processed = min(i + batch_size, len(items))
            if processed % 50 == 0 or processed == len(items):
                try:
                    await status_msg.edit_text(
                        f"⏳ Обработано {processed}/{len(items)} тикетов... "
                        f"Создано паттернов: {created}"
                    )
                except Exception:
                    pass

    elapsed = int(_time.monotonic() - t_start)
    counts = await count_solution_patterns_by_equipment()
    lines = [f"• {eq} — {cnt} паттернов" for eq, cnt in counts.items()]
    report = (
        f"✅ Анализ завершён за {elapsed} сек.\n"
        f"Обработано тикетов: {len(items)}\n"
        f"Создано паттернов: {created}\n"
        f"Пропущено (дубли/ошибки): {skipped}\n\n"
        f"По оборудованию:\n" + "\n".join(lines) + "\n\n"
        "Запусти /aianalyze снова чтобы обновить."
    )
    try:
        await status_msg.edit_text(report)
    except Exception:
        await message.answer(report)
```

Также добавить в начало файла если ещё нет: `import aiosqlite` и проверить что `from .. import db` импортирован.

- [ ] **Step 2: Добавить import aiosqlite в commands.py если отсутствует**

```bash
grep -n "import aiosqlite" bot/handlers/commands.py
```
Если нет — добавить в начало импортов.

- [ ] **Step 3: Добавить BotCommand в `bot/main.py`**

Найти список BotCommand и добавить после `aibackfill`:

```python
        BotCommand(command="aianalyze",  description="Извлечь паттерны решений из базы знаний"),
```

- [ ] **Step 4: Проверить синтаксис**

```bash
python -c "
import ast
for f in ['bot/handlers/commands.py', 'bot/main.py']:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"
```

- [ ] **Step 5: Commit**

```bash
git add bot/handlers/commands.py bot/main.py
git commit -m "feat: /aianalyze command — extract solution patterns from knowledge_items via Gemini"
```

---

## Task 8: Непрерывное усиление паттернов (_maybe_update_pattern)

**Files:**
- Modify: `bot/topic_manager.py`

- [ ] **Step 1: Добавить `_maybe_update_pattern` перед `_implicit_feedback`**

Найти функцию `_implicit_feedback` в `bot/topic_manager.py` и вставить перед ней:

```python
async def _maybe_update_pattern(title: str, staff_text: str, ticket_id: str) -> None:
    """Strengthen existing pattern or create new one from high-quality operator reply."""
    from .ai_summary import _detect_equipment
    from . import db as _db3
    import difflib

    equipment = _detect_equipment(title, staff_text)

    # Check if similar pattern exists → increment use_count
    patterns = await _db3.list_solution_patterns(limit=100)
    for p in patterns:
        if p["equipment"] != equipment:
            continue
        ratio = difflib.SequenceMatcher(
            None, p["problem_type"].lower(), title.lower()
        ).ratio()
        if ratio >= 0.7:
            await _db3.increment_pattern_use(p["id"])
            logger.debug(
                "Pattern %d reinforced for ticket %s (ratio=%.2f)",
                p["id"], ticket_id, ratio,
            )
            return

    # No existing pattern — extract new one via Gemini (non-fatal)
    import aiohttp as _aiohttp
    from .config import config as _cfg
    if not _cfg.gemini_api_key:
        return

    prompt = (
        "Из ответа технического специалиста извлеки:\n"
        "- problem_type: тип проблемы (5-10 слов)\n"
        "- steps: шаги решения через →\n"
        "Верни JSON: {\"problem_type\": ..., \"steps\": ...}\n"
        "Только JSON. Если шагов нет — верни {}.\n\n"
        f"Тема: {title}\nОтвет специалиста: {staff_text[:600]}"
    )
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "gemini-2.5-flash:generateContent"
                ),
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300},
                },
                params={"key": _cfg.gemini_api_key},
                timeout=_aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        import re as _re, json as _json
        raw = _re.sub(r"^```[^\n]*\n?", "", raw).rstrip("`").strip()
        parsed = _json.loads(raw)
        pt = (parsed.get("problem_type") or "").strip()
        st = (parsed.get("steps") or "").strip()
        if pt and st and not await _db3.pattern_exists_similar(equipment, pt):
            await _db3.save_solution_pattern(
                problem_type=pt, steps=st, source="implicit", equipment=equipment
            )
            logger.info(
                "New implicit pattern created for ticket %s: %r", ticket_id, pt
            )
    except Exception as exc:
        logger.warning("_maybe_update_pattern Gemini call failed: %s", exc)
```

- [ ] **Step 2: Вызвать `_maybe_update_pattern` из `_implicit_feedback`**

Найти блок `if ratio >= 0.7:` в `_implicit_feedback` и добавить вызов после сохранения в knowledge:

```python
    if ratio >= 0.7:
        await db.delete_ai_feedback_pending(record.topic_id)
        content = f"Тема: {pending['title']}\n\n{pending['history']}"
        from .knowledge.indexer import index_knowledge_item
        await index_knowledge_item(
            source="implicit_good",
            content=content,
            ticket_id=pending["ticket_id"],
            title=pending["title"],
            quality="good",
        )
        logger.info(
            "Implicit 👍 for ticket %s (ratio=%.2f)", pending["ticket_id"], ratio
        )
        # Reinforce or create solution pattern (only for high-confidence matches)
        if ratio >= 0.85:
            try:
                await _maybe_update_pattern(pending["title"], clean_staff, pending["ticket_id"])
            except Exception as exc:
                logger.warning("Pattern update failed: %s", exc)
```

- [ ] **Step 3: Проверить синтаксис**

```bash
python -c "import ast; ast.parse(open('bot/topic_manager.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 4: Запустить все тесты**

```bash
pytest tests/ -v
```
Ожидаем: все тесты PASSED, нет регрессий.

- [ ] **Step 5: Commit**

```bash
git add bot/topic_manager.py
git commit -m "feat: continuous pattern reinforcement via _maybe_update_pattern in implicit feedback"
```

---

## Финальная проверка

- [ ] Запустить полный тест-сьют: `pytest tests/ -v`
- [ ] Проверить синтаксис всех изменённых файлов: `python -c "import ast; [ast.parse(open(f, encoding='utf-8').read()) for f in ['bot/db.py','bot/ai_summary.py','bot/knowledge/store.py','bot/knowledge/indexer.py','bot/topic_manager.py','bot/handlers/commands.py','bot/main.py']]; print('All OK')"`
- [ ] Git push: `git push origin main`

---

## Что делать вручную после деплоя

1. `git pull && systemctl restart hde-bot` на VPS
2. Написать боту `/aianalyze` — дождаться отчёта (3-5 минут)
3. Прочитать отчёт в Telegram — убедиться что паттерны разумные
4. Готово. Система улучшается сама через implicit feedback.
