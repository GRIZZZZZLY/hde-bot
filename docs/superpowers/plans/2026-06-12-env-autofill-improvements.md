# Env Autofill Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Поднять точность автозаполнения поля «Окружение» (field 2) за счёт 6 улучшений: заголовок тикета в промпте, обогащённая история (фото-описания + аудио-транскрипты), детерминированный keyword pre-pass, реклассификация при новых сообщениях клиента, логирование правок операторов, приор по компании.

**Architecture:** Вся классификация остаётся в `bot/ticket_fields.py`. Новая колонка `env_option_id` в `ticket_topics` (NULL = не классифицировали, `''` = «не определено», цифры = option id) — даёт состояние для реклассификации, приора по компании и сverki правок оператора. Хуки: `_handle_client_reply_locked` (retry) и `_handle_ticket_closed_locked` (correction log) в `bot/topic_manager.py`.

**Tech Stack:** Python 3.11, aiosqlite, aiohttp, Groq llama-3.3-70b (fallback Llama 4 Scout), Deepgram (уже в `ai_summary._transcribe_audio_posts`), Vision-описания уже в `ticket_topics.photo_descriptions`.

---

### Task 1: DB — колонка env_option_id + приор по компании

**Files:**
- Modify: `bot/db/core.py` (TICKET_TOPIC_COLUMNS ~63, UPDATABLE_FIELDS ~86, TicketTopic ~111, _row_to_topic ~572)
- Modify: `bot/db/topics.py` (новая функция)
- Modify: `bot/db/__init__.py` (экспорт)
- Test: `tests/test_db_topics.py` (или ближайший существующий файл тестов БД)

- [ ] **Step 1: Написать падающий тест**

```python
async def test_env_option_id_roundtrip(tmp_db):
    await db.upsert_topic("t1", 100, company_name="ООО Ромашка")
    await db.update_topic("t1", env_option_id="145")
    rec = await db.get_topic("t1")
    assert rec.env_option_id == "145"

async def test_get_common_env_for_company(tmp_db):
    for i, env in enumerate(["146", "146", "145"]):
        await db.upsert_topic(f"t{i}", 100 + i, company_name="ООО Ромашка")
        await db.update_topic(f"t{i}", env_option_id=env)
    await db.upsert_topic("t9", 999, company_name="ООО Ромашка")
    assert await db.get_common_env_for_company("ООО Ромашка", exclude_ticket_id="t9") == "146"
    assert await db.get_common_env_for_company("", exclude_ticket_id="t9") is None
```

- [ ] **Step 2: Реализация**

`core.py`: добавить `"env_option_id": "TEXT"` в `TICKET_TOPIC_COLUMNS`; `"env_option_id"` в `UPDATABLE_FIELDS`; поле `env_option_id: Optional[str] = None` в `TicketTopic` (после `photo_descriptions`); в `_row_to_topic`: `env_option_id=row["env_option_id"] if "env_option_id" in row.keys() else None`.

`topics.py`:
```python
async def get_common_env_for_company(
    company_name: str, exclude_ticket_id: str = "", limit: int = 10
) -> Optional[str]:
    """Самое частое непустое env_option_id среди последних топиков компании."""
    if not company_name.strip():
        return None
    async with aiosqlite.connect(db_path()) as db:
        async with db.execute(
            """
            SELECT env_option_id, COUNT(*) AS cnt FROM (
                SELECT env_option_id FROM ticket_topics
                WHERE company_name = ? AND ticket_id != ?
                  AND env_option_id IS NOT NULL AND env_option_id != ''
                ORDER BY updated_at DESC LIMIT ?
            ) GROUP BY env_option_id ORDER BY cnt DESC LIMIT 1
            """,
            (company_name, exclude_ticket_id, limit),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None
```

`__init__.py`: добавить `get_common_env_for_company` в импорт из `.topics`.

- [ ] **Step 3: pytest, коммит**

### Task 2: Keyword pre-pass в ticket_fields.py

**Files:**
- Modify: `bot/ticket_fields.py`
- Test: `tests/test_ticket_fields.py`

- [ ] **Step 1: Падающие тесты**

```python
def test_keyword_match_single_brand():
    assert _keyword_match("касса атол не печатает чек") == "145"

def test_keyword_match_two_brands_ambiguous():
    assert _keyword_match("эвотор подключён к атол") is None

def test_keyword_match_shtrihkod_not_shtrih():
    assert _keyword_match("сканер штрихкодов не читает") == "154"

def test_keyword_match_nothing():
    assert _keyword_match("не работает программа") is None
```

- [ ] **Step 2: Реализация**

```python
# Deterministic keyword pre-pass: (regex, option_id). Run before the LLM.
_KEYWORD_PATTERNS: list[tuple[str, str]] = [
    (r"\bатол\b|atol", "145"),
    (r"эвотор|evotor", "146"),
    (r"ккм[\s-]?сервер|kkm[\s-]?server", "147"),
    (r"\bакси\b|aqsi", "148"),
    (r"viki\s*print|вики\s*принт", "156"),
    (r"веб[\s-]?касс|web[\s-]?касс", "157"),
    (r"(?:терминал|эквайринг)[^.\n]{0,40}(?:сбер|sber)|(?:сбер|sber)[^.\n]{0,40}(?:терминал|эквайринг)", "149"),
    (r"inpas|инпас", "150"),
    (r"штрих(?![\s-]?код)|shtrih", "155"),
    (r"принтер[\s\w]{0,20}чеков|чековый\s+принтер", "152"),
    (r"принтер[\s\w]{0,20}этикеток", "153"),
    (r"сканер", "154"),
]

def _keyword_match(text: str) -> str | None:
    """Option id, если в тексте однозначно упомянут ровно один бренд, иначе None."""
    low = text.lower()
    hits = {oid for pattern, oid in _KEYWORD_PATTERNS if re.search(pattern, low)}
    return hits.pop() if len(hits) == 1 else None
```

- [ ] **Step 3: pytest, коммит**

### Task 3: classify_environment — title + prior + pre-pass

**Files:**
- Modify: `bot/ticket_fields.py` (classify_environment, _groq_classify user content)
- Test: `tests/test_ticket_fields.py`

- [ ] **Step 1: Падающие тесты** (мок `_groq_classify`; проверить: keyword-шорткат не зовёт LLM; title и prior попадают в user content)

- [ ] **Step 2: Реализация**

```python
async def classify_environment(
    history: str, ticket_title: str = "", prior_hint: str = ""
) -> str | None:
    if not history.strip() and not ticket_title.strip():
        return None
    kw = _keyword_match(f"{ticket_title}\n{history}")
    if kw:
        logger.info("Env classifier: keyword pre-pass hit %s", kw)
        return kw
    parts = []
    if ticket_title.strip():
        parts.append(f"Тема тикета: {ticket_title.strip()}")
    if prior_hint:
        parts.append(f"Подсказка: у этого клиента ранее чаще всего определялось окружение «{prior_hint}». Используй как слабый приор, а не как ответ.")
    parts.append(f"Переписка:\n{history}")
    user_content = "\n\n".join(parts)
    raw = await _groq_classify(prompt, user_content)  # _groq_classify теперь принимает готовый user_content
    ...
```

`_groq_classify(prompt, user_content, model=...)` — убрать f"Переписка:\n{history}", передавать user_content как есть.

- [ ] **Step 3: pytest, коммит**

### Task 4: apply_ticket_fields — обогащение, сохранение env, retry, correction log

**Files:**
- Modify: `bot/ticket_fields.py`
- Test: `tests/test_ticket_fields.py`

- [ ] **Step 1: Падающие тесты** (apply сохраняет env_option_id в БД; фото-описания и транскрипты попадают в history; retry_env_classification обновляет только поле 2 и шлёт сообщение)

- [ ] **Step 2: Реализация**

Новая сигнатура: `apply_ticket_fields(bot, ticket_id, topic_id, history, ticket_title="", posts=None)`.

Внутри перед классификацией:
```python
record = None
try:
    from . import db as _db
    record = await _db.get_topic(ticket_id)
except Exception as exc:
    logger.warning("apply_ticket_fields: topic read failed: %s", exc)

enriched = history
if record and record.photo_descriptions:
    enriched += f"\n[Описание фото из тикета: {record.photo_descriptions}]"
if posts:
    try:
        from .ai_summary import _transcribe_audio_posts
        async with aiohttp.ClientSession() as session:
            for t in await _transcribe_audio_posts(posts, session):
                enriched += f"\n[Голосовое сообщение клиента: {t}]"
    except Exception as exc:
        logger.warning("apply_ticket_fields: transcription failed: %s", exc)

prior_hint = ""
if record and record.company_name:
    prior_id = await _db.get_common_env_for_company(record.company_name, exclude_ticket_id=ticket_id)
    if prior_id and prior_id in OKRUZHENIE_OPTIONS:
        prior_hint = OKRUZHENIE_OPTIONS[prior_id]

env_id = await classify_environment(enriched, ticket_title, prior_hint)
```

После PUT (удачного): `await _db.update_topic(ticket_id, env_option_id=env_id or "")` (try/except).

Retry-функция:
```python
async def retry_env_classification(bot: Bot, ticket_id: str, topic_id: int) -> None:
    """Повторная классификация окружения после нового сообщения клиента.

    Вызывается только когда прошлая попытка дала «не определено». Тихая: при
    неудаче ничего не пишет в топик (предупреждение уже было).
    """
    # fetch info/posts/comments → _build_history_text → apply-подобное обогащение →
    # classify_environment; если найдено: client.update_ticket_fields(ticket_id, {FIELD_OKRUZHENIE: env_id}),
    # db.update_topic(env_option_id=env_id), сообщение в топик
    # f"🧩 Окружение определено по новым сообщениям: {OKRUZHENIE_OPTIONS[env_id]}"
```

Correction log:
```python
ENV_CORRECTIONS_PATH = "data/env_corrections.jsonl"

async def log_env_outcome(ticket_id: str, predicted: str | None) -> None:
    """При закрытии тикета: сравнить наш прогноз с финальным значением поля 2."""
    # client.get_ticket_field_value(ticket_id, int(FIELD_OKRUZHENIE)) → final (int|None)
    # append jsonl: {ts, ticket_id, predicted, final, match: bool}
    # никогда не raise
```

- [ ] **Step 3: pytest, коммит**

### Task 5: Прокинуть title/posts из вызывающих мест

**Files:**
- Modify: `bot/topic_history.py:266-271`
- Modify: `bot/handlers/commands.py` (cmd_autofill ~115-137)
- Test: `tests/test_topic_manager.py` / существующие тесты autofill

- [ ] topic_history: `await apply_ticket_fields(bot, ticket_id, topic_id, autofill_history, ticket_title=ticket_title, posts=all_posts)`
- [ ] commands: `ticket_title=context.record.ticket_name or ""`, `posts=all_posts`
- [ ] pytest, коммит

### Task 6: Хуки в topic_manager

**Files:**
- Modify: `bot/topic_manager.py` (`_handle_client_reply_locked` ~510-566, `_handle_ticket_closed_locked` ~727-759)
- Test: `tests/test_topic_manager.py`

- [ ] client_reply: в конце `_handle_client_reply_locked`, если `record.env_option_id == ""` (была попытка, не определено):
```python
from .ticket_fields import retry_env_classification
asyncio.create_task(retry_env_classification(bot, record.ticket_id, record.topic_id))
```
- [ ] ticket_closed: в `_handle_ticket_closed_locked` перед `_delete_topic_now`, если `record.env_option_id is not None`:
```python
from .ticket_fields import log_env_outcome
try:
    await log_env_outcome(ticket_id, record.env_option_id or None)
except Exception as exc:
    logger.warning("env outcome log failed for %s: %s", ticket_id, exc)
```
- [ ] pytest, коммит

### Task 7: Полный прогон, деплой

- [ ] `python -m pytest` — все зелёные
- [ ] Коммит, push в main
- [ ] Деплой на VPS: ssh pull + systemctl restart (по памяти project_deploy_vps)
