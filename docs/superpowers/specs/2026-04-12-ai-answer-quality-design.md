# AI Answer Quality — Design Spec

**Date:** 2026-04-12  
**Status:** Approved

## Goal

Повысить точность и релевантность AI-подсказок для специалиста 2-й линии поддержки кассового оборудования. Сейчас AI даёт общие советы, не знает бренд оборудования и пишет как будто общается с клиентом, а не с техническим специалистом.

## Context

- Оператор: специалист 2-й линии по подключению и настройке кассового оборудования
- Оборудование: АТОЛ, Эвотор, Штрих-М, Viki + нефискальное (принтеры чеков, сканеры, весы, эквайринг Сбер/ВТБ/Тинькофф)
- Тикеты передаются с 1-й линии — базовая диагностика уже проведена
- Частые категории: подключение/настройка, ошибки ОФД/ФН, сетевые проблемы, эквайринг
- AI-ответ — инструкция для специалиста, НЕ текст для отправки клиенту

## Architecture

```
bot/db.py                  — таблица solution_patterns, CRUD
bot/ai_summary.py          — equipment detection, pattern lookup,
                             новый system prompt, confidence score
bot/knowledge/store.py     — company-буст в RRF (find_similar)
bot/knowledge/indexer.py   — передавать company_id в get_rag_context
bot/handlers/commands.py   — команда /aianalyze
bot/main.py                — зарегистрировать BotCommand /aianalyze
```

## Tech Stack

Python 3.10+, aiosqlite, aiohttp, Gemini API (gemini-2.5-flash), aiogram 3.x, difflib (stdlib), re (stdlib)

---

## Component 1 — Таблица `solution_patterns`

### Схема

```sql
CREATE TABLE IF NOT EXISTS solution_patterns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment    TEXT,           -- 'АТОЛ', 'Эвотор', NULL = любое
    problem_type TEXT NOT NULL,  -- 'ошибка ОФД', 'замена ФН'
    steps        TEXT NOT NULL,  -- '1. Открыть меню ФН → 2. ...'
    source       TEXT NOT NULL,  -- 'analyze' | 'implicit'
    use_count    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
```

Индексы: `(equipment, problem_type)` для быстрого поиска.

### CRUD функции в `bot/db.py`

- `save_solution_pattern(equipment, problem_type, steps, source) -> int`
- `find_solution_pattern(equipment, keywords) -> dict | None` — ищет по (equipment MATCH + problem_type keywords), сортирует по use_count DESC, возвращает один лучший
- `increment_pattern_use(pattern_id)` — `use_count += 1`
- `list_solution_patterns(limit=50) -> list[dict]` — для /aianalyze отчёта
- `pattern_exists_similar(equipment, problem_type) -> bool` — проверка дубликата перед сохранением

---

## Component 2 — Детекция оборудования

### Функция `_detect_equipment(title, history)` в `bot/ai_summary.py`

Keyword matching через regex — первое совпадение побеждает:

```python
_EQUIPMENT_PATTERNS = [
    (r'\bатол\b|atol|\bфр\b', 'АТОЛ'),
    (r'\bэвотор\b|evotor', 'Эвотор'),
    (r'\bштрих\b|shtrih|shtrikh', 'Штрих-М'),
    (r'\bviki\b|вики', 'Viki'),
    (r'\bсбер\b|sber|сбербанк', 'Эквайринг Сбер'),
    (r'\bвтб\b|vtb', 'Эквайринг ВТБ'),
    (r'\bтинькофф\b|tinkoff|тиньков', 'Эквайринг Тинькофф'),
    (r'\bптк\b|ptkf', 'ПТК'),
]
```

Ищет в `(title + " " + history[:300]).lower()`. Возвращает строку или `None`.

---

## Component 3 — Специализированный system prompt (Вариант A)

### Изменения в `_build_system_prompt` (`bot/ai_summary.py`)

Сигнатура расширяется: `_build_system_prompt(ticket_title, rag_examples, wiki_context, equipment, solution_steps)`

**Новый базовый блок:**

```
Ты — помощник технического специалиста 2-й линии поддержки.
Специализация: кассовое оборудование (АТОЛ, Эвотор, Штрих-М, Viki),
фискальные регистраторы, ОФД/ФН, сетевые подключения, эквайринг
(Сбер, ВТБ, Тинькофф).

Тикет передан с 1-й линии — базовую диагностику уже провели.

ВАЖНО: ты подсказываешь СПЕЦИАЛИСТУ что делать, не пишешь ответ клиенту.
Ответ — техническая инструкция к выполнению.
```

**Блок оборудования** (если `equipment` определён):
```
Оборудование в тикете: {equipment}
```

**Блок паттерна** (если `solution_steps` найден):
```
Типовые шаги решения для этого типа проблемы:
{solution_steps}
```

**Формат вывода** (заменяет текущий `_FORMAT_INSTRUCTIONS`):
```
Ответь РОВНО двумя строками:
Суть: <диагноз проблемы, бренд/модель если известны>
Ответ: <конкретные технические шаги через →>

Пример:
Суть: АТОЛ 30Ф — ошибка связи с ОФД, истёк сертификат.
Ответ: Меню ФН → Диагностика ОФД → Обновить сертификат в ЛК ОФД → Перерегистрация.

ВАЖНО: шаги для специалиста, не для клиента. Не используй markdown.
```

---

## Component 4 — Инъекция паттерна (Вариант B)

### В `generate_ticket_summary` (`bot/ai_summary.py`)

После получения `history` и до сборки промпта:

```python
equipment = _detect_equipment(ticket_title, history)
pattern = await db.find_solution_pattern(equipment, ticket_title)
solution_steps = pattern["steps"] if pattern else None
if pattern:
    await db.increment_pattern_use(pattern["id"])
```

`solution_steps` передаётся в `_build_system_prompt`.

---

## Component 5 — Company-фильтрация RAG

### Изменения в `find_similar` (`bot/knowledge/store.py`)

Добавить параметр `company_id: str = ""`.

В RRF-слиянии: если `company_id` задан, элементы из той же компании получают дополнительный бонус `+1/(k+1)` — эквивалент нахождения на 1-м месте в третьем списке. Не фильтрует жёстко, только приоритизирует.

### Изменения в `get_rag_context` (`bot/knowledge/indexer.py`)

Принимает `company_id: str = ""`, пробрасывает в `find_similar`.

### В `generate_ticket_summary` (`bot/ai_summary.py`)

Добавить параметр `company_id: str = ""`, передавать в `get_rag_context`.

Вызов в `topic_manager.py` обновить чтобы передавать `company_id` из payload.

---

## Component 6 — Confidence score

### Изменения в `find_similar` (`bot/knowledge/store.py`)

Возвращать `list[tuple[KnowledgeItem, float]]` вместо `list[KnowledgeItem]`. Float — cosine similarity score.

### В `get_rag_context` (`bot/knowledge/indexer.py`)

Возвращать `tuple[list[str], float]` — (examples, max_confidence_pct как int 0-100).

### В `generate_ticket_summary` (`bot/ai_summary.py`)

Возвращать `tuple[str, str, int] | None` — `(suit_line, answer_line, confidence_pct)`.

Confidence не включается в текст сообщения если < 40% (AI угадывает, не вводить в заблуждение).

### В `topic_manager.py`

Сообщение «Суть» рендерится как:
```python
# confidence >= 40%
f"🧠 <b>Суть ({confidence_pct}%):</b> {escape(suit_line)}"
# confidence < 40%
f"🧠 <b>Суть:</b> {escape(suit_line)}"
```

---

## Component 7 — Команда `/aianalyze`

### Логика (`bot/handlers/commands.py`)

1. Читать все `knowledge_items` где `source IN ('hde_closed', 'feedback', 'implicit_good')` и `quality != 'bad'`
2. Батчи по 15 записей
3. Для каждого батча — запрос к Gemini:
   ```
   Из каждого тикета извлеки: equipment (бренд или null),
   problem_type (краткое описание типа проблемы),
   steps (конкретные шаги решения через →).
   
   Верни JSON-массив: [{"equipment": ..., "problem_type": ..., "steps": ...}, ...]
   Только тикеты где есть явные шаги решения. Пропусти общие вопросы.
   ```
4. Дедупликация: перед сохранением проверять `pattern_exists_similar` (FTS5 поиск по problem_type, порог совпадения 0.7)
5. Минимальный порог: паттерн сохраняется только если встретился в 2+ тикетах батча ИЛИ это уникальный конкретный случай с кодом ошибки
6. Прогресс: сообщение в чат обновляется каждые 50 тикетов: `⏳ Обработано 150/847...`
7. Итоговый отчёт в Telegram:
   ```
   ✅ Анализ завершён за 3 мин.
   Обработано тикетов: 847
   Создано паттернов: 34
   
   По оборудованию:
   • АТОЛ — 18 паттернов
   • Эвотор — 9 паттернов
   • Штрих-М — 4 паттерна
   • Без бренда — 3 паттерна
   
   Запусти /aianalyze снова чтобы обновить.
   ```

---

## Component 8 — Непрерывное усиление (Элемент C)

### В `_implicit_feedback` (`bot/topic_manager.py`)

После сохранения `implicit_good` с `ratio >= 0.85`:

```python
# Попытаться усилить или создать паттерн из ответа оператора
await _maybe_update_pattern(pending["title"], clean_staff, pending["ticket_id"])
```

### Новая функция `_maybe_update_pattern`

1. Детектируем оборудование из `title + staff_text`
2. Ищем похожий паттерн в `solution_patterns` (FTS5 по problem_type)
3. Если нашли (similarity > 0.7) → `use_count += 1`
4. Если не нашли → вызов Gemini: *«Из этого ответа специалиста извлеки тип проблемы и шаги решения. Верни JSON.»*
5. Сохранить новый паттерн с `source='implicit'`
6. Всё non-fatal, exceptions логируются как WARNING

---

## Что делать вручную после деплоя

1. `git pull && systemctl restart hde-bot` на VPS
2. Написать боту `/aianalyze` — подождать 3-5 минут
3. Прочитать отчёт в Telegram — убедиться что паттерны разумные
4. Готово. Дальше система улучшается сама через implicit feedback.

---

## Не входит в этот спек

- Knowledge Management (дедупликация, expiry, /aimetrics) — отдельный спек
- Quick Replies — отдельный спек
- HDE Status Updates — отдельный спек

---

## Проверка (Verification)

1. Создать тикет с "АТОЛ" в заголовке → в саммари видно оборудование в «Суть»
2. После `/aianalyze` → запрос с АТОЛ + ОФД должен получить паттерн из таблицы
3. Confidence score показывается если в базе есть похожие примеры
4. Оператор пишет ответ аналогичный AI → через минуту `use_count` паттерна вырос
5. `/aistatus` показывает новый счётчик `solution_patterns`
