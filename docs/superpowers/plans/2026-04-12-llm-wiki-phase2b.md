# LLM Wiki (Phase 2B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an LLM Wiki system that synthesizes knowledge items (from 👍/✏️ feedback and /aiimport) into structured markdown articles, then injects relevant articles into AI summary prompts.

**Architecture:** `bot/wiki/builder.py` calls Gemini twice per item — once to extract a canonical topic, once to create/update the article in `data/wiki/{slug}.md`. `bot/wiki/searcher.py` finds the best-matching article via word-overlap and returns it as context for `ai_summary.py`. Integration points: cb_ai_good, capture_correction, cmd_aiimport, generate_ticket_summary.

**Tech Stack:** Python asyncio, aiohttp (Gemini API, same pattern as ai_summary.py), stdlib json/os/re for wiki index, pytest-asyncio for tests.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `bot/wiki/__init__.py` | Create (empty) | Package marker |
| `bot/wiki/builder.py` | Create | Gemini calls → create/update `data/wiki/*.md` |
| `bot/wiki/searcher.py` | Create | Word-overlap search → return article text |
| `bot/handlers/ai_feedback.py` | Modify | Call builder after 👍 and after ✏️ correction |
| `bot/handlers/commands.py` | Modify | Call builder after each /aiimport ticket |
| `bot/ai_summary.py` | Modify | Call searcher + pass wiki_context to prompt |
| `tests/test_llm_wiki.py` | Create | Tests for builder helpers and searcher |

---

## Task 1: bot/wiki/builder.py

**Files:**
- Create: `bot/wiki/__init__.py`
- Create: `bot/wiki/builder.py`
- Test: `tests/test_llm_wiki.py`

- [ ] **Step 1: Create empty `bot/wiki/__init__.py`**

```bash
touch bot/wiki/__init__.py
```

- [ ] **Step 2: Write failing tests for `_topic_slug` and `_match_score` helpers**

Create `tests/test_llm_wiki.py`:

```python
"""Tests for LLM Wiki builder and searcher."""
from __future__ import annotations
import json
import os
import pytest


def test_topic_slug_stable():
    from bot.wiki.builder import _topic_slug
    assert _topic_slug("Авторизация") == _topic_slug("Авторизация")
    assert _topic_slug("авторизация") == _topic_slug("АВТОРИЗАЦИЯ")  # case-insensitive
    assert len(_topic_slug("any topic")) == 12


def test_topic_slug_different_topics():
    from bot.wiki.builder import _topic_slug
    assert _topic_slug("Авторизация") != _topic_slug("Принтер")


def test_match_score_overlap():
    from bot.wiki.searcher import _match_score
    assert _match_score("ошибка авторизации пароль", "авторизация пользователя") > 0
    assert _match_score("принтер АТОЛ", "кассовый аппарат") == 0


def test_load_index_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.wiki.builder._INDEX_PATH", str(tmp_path / "missing.json"))
    from bot.wiki.builder import _load_index
    assert _load_index() == {}


def test_save_and_load_index(tmp_path, monkeypatch):
    index_path = str(tmp_path / "index.json")
    wiki_dir = str(tmp_path)
    monkeypatch.setattr("bot.wiki.builder._INDEX_PATH", index_path)
    monkeypatch.setattr("bot.wiki.builder._WIKI_DIR", wiki_dir)
    from bot.wiki.builder import _save_index, _load_index
    data = {"abc123": {"topic": "Авторизация", "updated": "2026-04-12T00:00:00"}}
    _save_index(data)
    loaded = _load_index()
    assert loaded["abc123"]["topic"] == "Авторизация"
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_llm_wiki.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.wiki'`

- [ ] **Step 4: Create `bot/wiki/__init__.py`**

```bash
echo "" > bot/wiki/__init__.py
```

- [ ] **Step 5: Create `bot/wiki/builder.py`**

```python
"""LLM Wiki Builder: synthesize knowledge items into structured markdown articles."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

import aiohttp

from ..config import config

logger = logging.getLogger(__name__)

_WIKI_DIR = "data/wiki"
_INDEX_PATH = f"{_WIKI_DIR}/_index.json"
_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)


def _topic_slug(topic: str) -> str:
    """Convert topic name to a stable 12-char hex slug (MD5-based)."""
    return hashlib.md5(topic.strip().lower().encode()).hexdigest()[:12]


def _load_index() -> dict:
    """Load wiki index (slug → {topic, updated})."""
    if not os.path.exists(_INDEX_PATH):
        return {}
    try:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(index: dict) -> None:
    os.makedirs(os.path.dirname(_INDEX_PATH), exist_ok=True)
    with open(_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def _article_path(slug: str) -> str:
    return f"{_WIKI_DIR}/{slug}.md"


async def _call_gemini(prompt: str) -> str | None:
    """Send a single-turn prompt to Gemini and return text or None."""
    if not config.gemini_api_key:
        return None
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _GEMINI_URL,
                json=payload,
                params={"key": config.gemini_api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Gemini wiki error %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        logger.warning("Gemini wiki call failed: %s", exc)
        return None


async def _extract_topic(title: str, content: str) -> str | None:
    """Ask Gemini for a canonical topic name (2–5 words in Russian)."""
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        "Определи тему этого тикета — краткое название 2–5 слов на русском.\n"
        "Верни ТОЛЬКО тему, без пояснений.\n\n"
        f"Тикет: «{title}»\n\n{content[:800]}"
    )
    topic = await _call_gemini(prompt)
    if topic:
        return re.sub(r'^[«"\']+|[»"\']+$', "", topic.strip())
    return None


async def _create_article(topic: str, title: str, content: str) -> str | None:
    """Ask Gemini to write a new wiki article from a ticket."""
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        f"Создай wiki-статью на тему: «{topic}»\n\n"
        "Используй тикет как основу. Формат: markdown с разделами:\n"
        "## Типичные причины\n## Решения\n## Примечания\n\n"
        "Без лишних слов, только суть.\n\n"
        f"Тикет:\nТема: {title}\n\n{content[:1500]}"
    )
    return await _call_gemini(prompt)


async def _update_article(existing: str, title: str, content: str) -> str | None:
    """Ask Gemini to update an existing wiki article with new information."""
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        "Обнови wiki-статью, интегрировав новые данные из тикета.\n"
        "Добавь новые паттерны или уточни существующие.\n"
        "Верни ТОЛЬКО обновлённый markdown, без пояснений.\n\n"
        f"Существующая статья:\n{existing}\n\n"
        f"---\n\nНовый тикет:\nТема: {title}\n\n{content[:1200]}"
    )
    return await _call_gemini(prompt)


async def build_or_update_wiki_article(
    title: str,
    content: str,
    ticket_id: str = "",
) -> str | None:
    """Create or update a wiki article for the given knowledge item.

    Returns the article slug on success, None if skipped or failed.
    Errors are logged but never raised — callers should use try/except.
    """
    if not config.gemini_api_key:
        logger.debug("Wiki build skipped: no Gemini API key")
        return None

    os.makedirs(_WIKI_DIR, exist_ok=True)

    topic = await _extract_topic(title, content)
    if not topic:
        logger.warning("Wiki: could not extract topic for ticket %s", ticket_id)
        return None

    slug = _topic_slug(topic)
    path = _article_path(slug)

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            existing = ""
        article_text = await _update_article(existing, title, content)
        if not article_text:
            return None
        logger.info("Wiki: updated '%s' (ticket %s)", topic, ticket_id)
    else:
        body = await _create_article(topic, title, content)
        if not body:
            return None
        now = datetime.now(timezone.utc).isoformat()
        article_text = (
            f"<!-- topic: {topic} -->\n"
            f"<!-- created: {now} -->\n\n"
            f"# {topic}\n\n{body}"
        )
        logger.info("Wiki: created '%s' (ticket %s)", topic, ticket_id)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(article_text)
    except OSError as exc:
        logger.error("Wiki: could not write %s: %s", path, exc)
        return None

    index = _load_index()
    index[slug] = {"topic": topic, "updated": datetime.now(timezone.utc).isoformat()}
    _save_index(index)

    return slug
```

- [ ] **Step 6: Run tests — should pass now**

```bash
pytest tests/test_llm_wiki.py::test_topic_slug_stable tests/test_llm_wiki.py::test_topic_slug_different_topics tests/test_llm_wiki.py::test_load_index_missing_file tests/test_llm_wiki.py::test_save_and_load_index -v
```
Expected: 4 PASSED

- [ ] **Step 7: Commit**

```bash
git add bot/wiki/__init__.py bot/wiki/builder.py tests/test_llm_wiki.py
git commit -m "feat: add LLM Wiki builder with Gemini-based article synthesis"
```

---

## Task 2: bot/wiki/searcher.py

**Files:**
- Create: `bot/wiki/searcher.py`
- Modify: `tests/test_llm_wiki.py` (add searcher tests)

- [ ] **Step 1: Write failing tests for searcher**

Append to `tests/test_llm_wiki.py`:

```python
def test_match_score_no_overlap():
    from bot.wiki.searcher import _match_score
    assert _match_score("принтер АТОЛ", "кассовый аппарат") == 0


def test_get_wiki_context_no_index(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr("bot.wiki.searcher._INDEX_PATH", str(tmp_path / "missing.json"))
    from bot.wiki.searcher import get_wiki_context
    result = asyncio.get_event_loop().run_until_complete(get_wiki_context("авторизация"))
    assert result is None


def test_get_wiki_context_finds_article(tmp_path, monkeypatch):
    import asyncio, json
    index_path = str(tmp_path / "_index.json")
    wiki_dir = str(tmp_path)
    monkeypatch.setattr("bot.wiki.searcher._INDEX_PATH", index_path)
    monkeypatch.setattr("bot.wiki.searcher._WIKI_DIR", wiki_dir)

    slug = "abc123def456"
    index = {slug: {"topic": "Авторизация пароль", "updated": "2026-04-12T00:00:00"}}
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    article_path = tmp_path / f"{slug}.md"
    article_path.write_text("## Решения\nОчистить кэш браузера.", encoding="utf-8")

    from bot.wiki.searcher import get_wiki_context
    result = asyncio.get_event_loop().run_until_complete(
        get_wiki_context("проблема с авторизацией паролем")
    )
    assert result is not None
    assert "Очистить кэш" in result


def test_get_wiki_context_no_match(tmp_path, monkeypatch):
    import asyncio, json
    index_path = str(tmp_path / "_index.json")
    wiki_dir = str(tmp_path)
    monkeypatch.setattr("bot.wiki.searcher._INDEX_PATH", index_path)
    monkeypatch.setattr("bot.wiki.searcher._WIKI_DIR", wiki_dir)

    slug = "abc123def456"
    index = {slug: {"topic": "Принтер АТОЛ", "updated": "2026-04-12T00:00:00"}}
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    from bot.wiki.searcher import get_wiki_context
    result = asyncio.get_event_loop().run_until_complete(
        get_wiki_context("проблема с кассой")
    )
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_llm_wiki.py::test_get_wiki_context_no_index -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.wiki.searcher'`

- [ ] **Step 3: Create `bot/wiki/searcher.py`**

```python
"""LLM Wiki Searcher: find the most relevant wiki article for a ticket topic."""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_WIKI_DIR = "data/wiki"
_INDEX_PATH = f"{_WIKI_DIR}/_index.json"
_MAX_ARTICLE_CHARS = 2000


def _load_index() -> dict:
    if not os.path.exists(_INDEX_PATH):
        return {}
    try:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize(text: str) -> set[str]:
    """Extract words ≥4 chars for fuzzy matching."""
    words = re.findall(r'[а-яёА-ЯЁa-zA-Z]+', text.lower())
    return {w for w in words if len(w) >= 4}


def _match_score(topic: str, query: str) -> int:
    """Count shared meaningful words between topic and query."""
    return len(_normalize(topic) & _normalize(query))


async def get_wiki_context(ticket_title: str) -> str | None:
    """Return the most relevant wiki article for the ticket title, or None.

    Uses word-overlap scoring — no Gemini call needed.
    """
    index = _load_index()
    if not index:
        return None

    best_slug: str | None = None
    best_score = 0
    for slug, meta in index.items():
        topic = meta.get("topic", "")
        score = _match_score(topic, ticket_title)
        if score > best_score:
            best_score = score
            best_slug = slug

    if best_score == 0 or best_slug is None:
        return None

    path = f"{_WIKI_DIR}/{best_slug}.md"
    if not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        logger.warning("Wiki: could not read %s: %s", path, exc)
        return None

    # Strip HTML comments (frontmatter)
    content = re.sub(r'<!--.*?-->\n?', '', content, flags=re.DOTALL).strip()

    if len(content) > _MAX_ARTICLE_CHARS:
        content = content[:_MAX_ARTICLE_CHARS] + "\n\n[...статья сокращена]"

    return content or None
```

- [ ] **Step 4: Run all searcher tests**

```bash
pytest tests/test_llm_wiki.py -v
```
Expected: all PASSED (8 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/wiki/searcher.py tests/test_llm_wiki.py
git commit -m "feat: add LLM Wiki searcher with word-overlap topic matching"
```

---

## Task 3: Integrate builder in ai_feedback.py

**Files:**
- Modify: `bot/handlers/ai_feedback.py` (lines ~62–70 and ~134–141)

The 👍 handler `cb_ai_good` is at line ~72. After the `await index_knowledge_item(...)` call (line ~63), add wiki update.
The `capture_correction` handler is at line ~122. After its `await index_knowledge_item(...)` call (line ~134), add wiki update.

- [ ] **Step 1: Read `bot/handlers/ai_feedback.py` lines 60–75 and 130–142**

Confirm exact location of both `index_knowledge_item` calls. Expected:
- cb_ai_good: `await index_knowledge_item(source="feedback", content=content, ...)`
- capture_correction: `await index_knowledge_item(source="feedback", content=content, ..., quality="corrected")`

- [ ] **Step 2: Add wiki call after 👍 in cb_ai_good**

After the `await index_knowledge_item(...)` block in `cb_ai_good`, add:

```python
    # Update wiki article (non-fatal)
    try:
        from ..wiki.builder import build_or_update_wiki_article
        await build_or_update_wiki_article(
            title=pending["title"],
            content=content,
            ticket_id=pending["ticket_id"],
        )
    except Exception as exc:
        logger.warning("Wiki update failed after 👍: %s", exc)
```

- [ ] **Step 3: Add wiki call after ✏️ correction in capture_correction**

After the `await index_knowledge_item(...)` block in `capture_correction`, add:

```python
    # Update wiki article (non-fatal)
    try:
        from ..wiki.builder import build_or_update_wiki_article
        await build_or_update_wiki_article(
            title=pending["title"],
            content=content,
            ticket_id=pending["ticket_id"],
        )
    except Exception as exc:
        logger.warning("Wiki update failed after correction: %s", exc)
```

- [ ] **Step 4: Run existing tests to verify no regressions**

```bash
pytest tests/ -v --ignore=tests/test_llm_wiki.py
```
Expected: all previously passing tests still PASS

- [ ] **Step 5: Commit**

```bash
git add bot/handlers/ai_feedback.py
git commit -m "feat: trigger wiki article update after 👍 and ✏️ feedback"
```

---

## Task 4: Integrate builder in /aiimport

**Files:**
- Modify: `bot/handlers/commands.py` (inside cmd_aiimport loop, after `added += 1`)

In `cmd_aiimport`, the loop saves each ticket and increments `added`. After `added += 1`, add:

- [ ] **Step 1: Find exact location in commands.py**

Search for `added += 1` inside `cmd_aiimport`. There are two branches (with embedding and fallback). Add wiki call after both `added += 1` lines.

- [ ] **Step 2: Add wiki call after both `added += 1` occurrences**

In both branches after `added += 1`, add:

```python
            # Update wiki article (non-fatal, synchronous per spec)
            try:
                from ...wiki.builder import build_or_update_wiki_article
                await build_or_update_wiki_article(
                    title=ticket_title,
                    content=content,
                    ticket_id=ticket_id,
                )
            except Exception as exc:
                logger.warning("Wiki update failed for ticket %s: %s", ticket_id, exc)
```

Note: `bot/handlers/commands.py` is 3 levels deep from `bot/`, so the import is `from ...wiki.builder`.
Actually, `commands.py` is at `bot/handlers/commands.py`, so relative to `bot/handlers/` it's `..` for `bot/`, then `.wiki.builder` → `from ..wiki.builder import build_or_update_wiki_article`.

- [ ] **Step 3: Verify import path is correct**

Check that `bot/wiki/builder.py` exists. From `bot/handlers/commands.py`:
- `..` = `bot/`
- `..wiki` = `bot/wiki`
- Import: `from ..wiki.builder import build_or_update_wiki_article`

- [ ] **Step 4: Run tests**

```bash
pytest tests/ -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add bot/handlers/commands.py
git commit -m "feat: trigger wiki article build during /aiimport"
```

---

## Task 5: Integrate searcher in ai_summary.py

**Files:**
- Modify: `bot/ai_summary.py` (function `_build_system_prompt` + `generate_ticket_summary`)

- [ ] **Step 1: Read `bot/ai_summary.py` lines 36–53 (\_build\_system\_prompt)**

Confirm current signature:
```python
def _build_system_prompt(ticket_title: str, rag_examples: list[str] | None = None) -> str:
```

- [ ] **Step 2: Add `wiki_context` parameter to `_build_system_prompt`**

Replace the function signature and body:

```python
def _build_system_prompt(
    ticket_title: str,
    rag_examples: list[str] | None = None,
    wiki_context: str | None = None,
) -> str:
    base = "Ты — ассистент технической поддержки.\n"
    if ticket_title:
        base += (
            f"Тема обращения: «{ticket_title}»\n\n"
            "Используй тему и примеры, чтобы предложить конкретный ответ, "
            "подходящий именно для этого типа проблемы.\n\n"
        )
    if wiki_context:
        base += (
            "Справочная статья из базы знаний по данной теме:\n\n"
            f"{wiki_context}\n\n"
            "---\n\n"
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

- [ ] **Step 3: Add wiki_context retrieval in `generate_ticket_summary`**

Find the line `system_text = _build_system_prompt(ticket_title, rag_examples or None)` (around line 128).

Before that line, add wiki retrieval:

```python
    # Wiki: find relevant article for this topic
    wiki_ctx: str | None = None
    try:
        from .wiki.searcher import get_wiki_context
        wiki_ctx = await get_wiki_context(ticket_title)
        if wiki_ctx:
            logger.info("Wiki context found for ticket %s (%d chars)", ticket_id, len(wiki_ctx))
    except Exception as exc:
        logger.warning("Wiki context retrieval failed: %s", exc)
```

Change the `_build_system_prompt` call to:

```python
    system_text = _build_system_prompt(ticket_title, rag_examples or None, wiki_ctx)
```

- [ ] **Step 4: Run full test suite**

```bash
pytest tests/ -v
```
Expected: all PASS

- [ ] **Step 5: Commit and push**

```bash
git add bot/ai_summary.py
git commit -m "feat: inject wiki article context into AI summary prompt"
git push
```

---

## Verification

1. Ensure `data/wiki/` dir is created on first run (builder calls `os.makedirs` automatically)
2. Нажать 👍 под саммари → в логах появится `Wiki: created 'Тема'` или `Wiki: updated 'Тема'`
3. `ls data/wiki/` → файл `{slug}.md` + `_index.json`
4. Создать тикет на похожую тему → в логах `Wiki context found for ticket ... (N chars)`
5. `/aiimport 5` → в логах 5 строк wiki update
6. Если `GEMINI_API_KEY` не настроен — всё молча пропускается, ошибок нет
