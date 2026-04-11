# AI Knowledge System: RAG + LLM Wiki Design

**Дата:** 2026-04-11  
**Статус:** Approved  
**Проект:** HDE Bot — AI-ассистент для технической поддержки

---

## Контекст и цель

Оператор обрабатывает 70–85 тикетов в неделю лично. AI саммари уже реализован
(Gemini, `bot/ai_summary.py`), но работает без контекста прошлых решений.

**Цель системы:** накапливать знания из реальной работы оператора и автоматически
улучшать качество саммари и предлагаемых ответов со временем. Система должна
оставаться работоспособной при смене LLM-провайдера.

---

## Архитектура: три слоя

### Слой 1 — Векторный индекс (SQLite)

Все знания хранятся в таблице `knowledge_items`. При генерации саммари система
находит топ-3 похожих прошлых случая и вставляет их как few-shot в промпт.

```
источники → knowledge_items → embedding (768 dim) → cosine search → промпт
```

### Слой 2 — LLM Wiki (`data/wiki/`)

Вместо хранения сырых чанков LLM **синтезирует знания** в структурированные
markdown-статьи. Статьи обновляются при каждом новом источнике знаний.

```
новый тикет/звонок/статья → LLM Wiki Builder → data/wiki/topic.md (обновление)
```

### Слой 3 — Сырые логи (`data/`)

```
data/ai_log.jsonl       — все генерации (для анализа и курации)
data/ai_examples.jsonl  — кураторские few-shot примеры (ручной отбор)
data/wiki/              — LLM Wiki: синтезированные markdown-статьи
data/wiki/_index.md     — оглавление с тегами и ссылками
```

---

## Источники знаний

| Источник | Как попадает в систему | source= |
|---|---|---|
| Ответы оператора `/send` | Автоматически при отправке | `feedback` |
| Оценка саммари 👍 | Кнопка под саммари | `feedback` |
| Исправление ✏️ | Диалог в топике | `corrected` |
| Макросы HDE | HDE API (разовый импорт) | `macro` |
| Внешние статьи | `/aidocs add URL` | `doc` |
| Транскрипции звонков | Аудиофайл в топике → Deepgram | `transcription` |
| База знаний Teamly | Playwright-скрапер | `teamly` |

---

## Структура файлов (новые модули)

```
bot/
├── knowledge/
│   ├── __init__.py
│   ├── store.py        — сохранить, найти похожие (cosine similarity, numpy)
│   ├── indexer.py      — pipeline: текст → embedding → store
│   └── sources.py      — адаптеры: HDE macros API, URL fetcher
├── wiki/
│   ├── __init__.py
│   ├── builder.py      — LLM Wiki Builder: интегрирует новые знания в статьи
│   └── searcher.py     — поиск по _index.md, загрузка статей по тегам
├── transcription.py    — Deepgram: аудио → текст
├── teamly_scraper.py   — Playwright-скрапер Teamly KB
├── ai_summary.py       — существующий + RAG context retrieval
└── handlers/
    ├── ai_feedback.py  — callback 👍/✏️/👎 + correction state machine
    └── commands.py     — +/aiknowledge, /aisync, /aidocs, /aireindex
```

---

## Схема БД (новые таблицы)

```sql
-- Векторный индекс знаний
CREATE TABLE knowledge_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    ticket_id  TEXT,
    title      TEXT,
    content    TEXT NOT NULL,      -- сырой текст (всегда сохраняется)
    embedding  BLOB,               -- float32 numpy bytes, 768 dims
    quality    TEXT DEFAULT 'good', -- 'good' | 'corrected' | 'bad'
    url        TEXT,               -- для teamly/doc источников
    content_hash TEXT,             -- для инкрементального синка
    created_at TEXT DEFAULT (datetime('now'))
);

-- Состояние ожидания исправления (✏️ кнопка)
CREATE TABLE ai_feedback_pending (
    message_id   INTEGER PRIMARY KEY,  -- Telegram message_id саммари
    topic_id     INTEGER NOT NULL,
    ticket_id    TEXT NOT NULL,
    history      TEXT NOT NULL,
    expires_at   TEXT NOT NULL         -- TTL 24 часа
);

-- Состояние синка Teamly
CREATE TABLE teamly_sync_state (
    url          TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    synced_at    TEXT NOT NULL,
    title        TEXT
);
```

---

## Генерация саммари (обновлённый поток)

```
1. Получить переписку тикета (posts + info)
2. Построить embedding запроса (ticket_title + последнее сообщение клиента)
3. knowledge/store.py: cosine search → топ-3 похожих knowledge_items
4. wiki/searcher.py: найти релевантную wiki-статью по тегам/теме
5. Собрать промпт:
   - system: роль + тема тикета
   - few-shot: топ-3 примера из knowledge_items
   - wiki-контекст: краткая выдержка из wiki-статьи (если найдена)
   - user: текущая переписка
6. Gemini: генерирует "Суть + Ответ"
7. Отправить в Telegram + кнопки [👍][✏️][👎]
8. Записать в ai_log.jsonl
```

---

## Inline Feedback: кнопки под саммари

```
[ 👍 Хороший ответ ]  [ ✏️ Исправить ]  [ 👎 Плохой ]
```

### 👍 Хороший ответ
- Сохранить историю + ответ в `knowledge_items` (quality='good')
- Сгенерировать embedding → сохранить
- LLM Wiki Builder: обновить wiki-статью по теме тикета
- Убрать кнопки. Никакого подтверждающего сообщения (тихо)

### ✏️ Исправить
- Убрать кнопки
- Бот: _"✏️ Введи правильный ответ клиенту — я сохраню его как пример"_
- Следующее сообщение оператора в этом топике = исправление
- Сохранить в `knowledge_items` (quality='corrected')
- Бот: _"✅ Сохранено как исправленный пример"_
- Запись в `ai_feedback_pending` с TTL 24 часа (если не ответил — удалить)

### 👎 Плохой ответ
- Убрать кнопки
- Пометить в `ai_log.jsonl` как bad
- Не индексировать. Никакого сообщения

---

## Транскрипция звонков (Deepgram)

**Рабочий процесс:**
1. Оператор звонит через MicroSIP, включает запись → `.wav` файл на ПК
2. Открывает топик тикета в Telegram
3. Отправляет `.wav`/`.mp3` файл как документ
4. Бот определяет: аудиофайл в топике с тикетом?
5. Скачивает файл, отправляет в Deepgram API (`language: ru`, модель `nova-3`)
6. Получает транскрипт → сохраняет в `knowledge_items` (source='transcription')
7. LLM Wiki Builder обновляет wiki-статью: добавляет раздел "Из звонков"
8. Бот отвечает: _"🎙️ Звонок транскрибирован (2:34) — добавлен в базу знаний"_

**Стоимость Deepgram:** ~$0.004/мин. При 2–3 звонках в день ~$2–3/месяц.  
**Deepgram не затрагивает LLM** — это отдельный слой.

---

## Teamly Scraper (`/aisync teamly`)

### Полный сценарий:

**Шаг 1 — Авторизация**
```
Credentials: TEAMLY_EMAIL + TEAMLY_PASSWORD из .env
Playwright: headless браузер → страница входа → ввод credentials → редирект
```

**Шаг 2 — Обход структуры**
```
Playwright: развернуть дерево навигации в сайдбаре
→ собрать все URL статей
→ сравнить с teamly_sync_state в БД
→ отфильтровать не изменившиеся (по content_hash)
→ сообщить: "📂 Найдено: 47 статей, новых/изменённых: 15"
```

**Шаг 3 — Скрапинг статей**
```
Для каждой новой/изменённой статьи:
  Playwright → перейти → дождаться загрузки → извлечь заголовок + текст
  Добавить задержку 500–1000 мс между запросами (не нагружать сервер)
  Прогресс: "📄 [12/15] Настройка принтера HP..."
```

**Шаг 4 — LLM Wiki Builder**
```
Для каждой статьи:
  Проверить data/wiki/ — есть ли статья по этой теме?
  Если нет → создать новую wiki-статью
  Если есть → интегрировать новую информацию (через Gemini)
  Обновить data/wiki/_index.md
  Сохранить в knowledge_items (source='teamly')
  Сгенерировать embedding
```

**Шаг 5 — Итог**
```
✅ Синхронизация Teamly завершена

📊 Результаты:
• Новых статей: 12
• Обновлено: 3
• Без изменений: 32
• Wiki-статей создано: 5, обновлено: 8
• Embeddings сгенерировано: 15
⏱ Время: 2м 34с
```

**Инкрементальность:** при повторном запуске обрабатываются только статьи,
у которых изменился `content_hash`. Данные о синке хранятся в `teamly_sync_state`.

**Автосинк:** опционально — запускать раз в сутки через планировщик бота
(аналогично дайджесту в `scheduler.py`).

**Обработка ошибок:** истекшая сессия → переавторизация (1 попытка).
Недоступная статья → пропустить, добавить в отчёт.

---

## Команды управления

| Команда | Действие |
|---|---|
| `/aisummary` | Статус AI саммари (включён/выключён) |
| `/aisummary on/off` | Включить/выключить саммари |
| `/aiknowledge` | Статистика базы знаний по источникам |
| `/aisync teamly` | Запустить Playwright-скрапер Teamly |
| `/aidocs add URL` | Проиндексировать внешнюю статью/FAQ |
| `/aireindex` | Пересоздать все embeddings (при смене LLM) |

### `/aiknowledge` пример вывода:
```
📚 База знаний: 234 записи

По источникам:
• 👍 feedback:      87  (оценённые ответы)
• ✏️ corrected:     23  (исправленные ответы)
• 🏢 teamly:        67  (база знаний компании)
• 📄 doc:           18  (внешние статьи)
• 🎙️ transcription: 12  (транскрипции звонков)
• 🔧 macro:         27  (макросы HDE)

Wiki-статей: 31
Последний синк Teamly: сегодня 09:15
```

---

## Провайдер-независимость

**Принцип:** сырой текст всегда сохраняется отдельно от embedding.

| Что хранится | Формат | При смене провайдера |
|---|---|---|
| `content` TEXT | Сырой текст | Не меняется |
| `embedding` BLOB | float32 bytes (текущий провайдер) | Пересоздать через `/aireindex` |
| `data/wiki/*.md` | Markdown | Не меняется |
| `data/ai_log.jsonl` | JSON | Не меняется |

**Смена провайдера = одна команда `/aireindex`:**
```
/aireindex
→ Загрузить все knowledge_items.content
→ Пересгенерировать embeddings новой моделью
→ Обновить BLOB колонки
→ "✅ Переиндексировано: 234 записи"
```

**Абстракция LLM клиента:** `bot/llm_client.py` — интерфейс с Gemini-реализацией.
Смена на OpenAI/Anthropic = замена одного файла.

---

## Переменные окружения (дополнения к .env)

```env
# Deepgram (транскрипция звонков)
DEEPGRAM_API_KEY=

# Teamly Knowledge Base
TEAMLY_EMAIL=
TEAMLY_PASSWORD=
TEAMLY_BASE_URL=https://posiflora.teamly.ru

# Опционально: автосинк Teamly (час UTC, -1 = выключено)
TEAMLY_SYNC_HOUR_UTC=7
```

---

## Фазы реализации

| Фаза | Что | Результат |
|---|---|---|
| **1** | `knowledge/store.py` + DB schema + embeddings | Векторный поиск работает |
| **2** | Feedback кнопки 👍/✏️/👎 + `ai_feedback.py` | Оператор оценивает саммари |
| **3** | RAG в `ai_summary.py` (few-shot из store) | Саммари использует прошлые примеры |
| **4** | `wiki/builder.py` + LLM Wiki pipeline | Wiki-статьи накапливаются |
| **5** | `transcription.py` (Deepgram) | Звонки транскрибируются |
| **6** | `teamly_scraper.py` + `/aisync teamly` | Teamly KB проиндексирован |
| **7** | `/aiknowledge`, `/aidocs`, `/aireindex` | Полное управление из Telegram |

---

## Проверка (Verification)

1. Создать тикет → появляется саммари с кнопками 👍/✏️/👎
2. Нажать 👍 → запись в `knowledge_items`, кнопки исчезли
3. Создать похожий тикет → в промпте видны few-shot примеры
4. Отправить `.wav` в топик → транскрипт + запись в БД
5. `/aisync teamly` → итоговый отчёт, статьи в `data/wiki/`
6. `/aireindex` → все embeddings пересозданы
7. Изменить `GEMINI_API_KEY` на другой провайдер, запустить `/aireindex` → качество сохраняется
