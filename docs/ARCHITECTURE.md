# HDE Telegram Bot — Полная архитектура

> Актуально на апрель 2026. Описывает систему от webhook до ответа клиенту.

---

## Что делает бот

Telegram-бот для специалиста 2-й линии поддержки кассового оборудования. Мост между HelpDeskEddy (HDE) и Telegram:

- Каждый тикет — отдельный **forum topic** в Telegram-группе
- Клиентские сообщения и вложения автоматически появляются в топике
- Оператор отвечает клиенту командами прямо из Telegram
- AI-подсказка при каждом новом тикете: диагноз + шаги решения для специалиста
- SLA-таймер: напоминание за 10 минут до истечения времени ответа
- База знаний с векторным поиском (RAG) на основе решённых тикетов

---

## Стек технологий

| Компонент | Технология |
|---|---|
| Telegram Bot API | aiogram 3.x (long polling) |
| HTTP сервер (HDE webhooks) | aiohttp |
| База данных | SQLite + aiosqlite (async) |
| AI-генерация | Google Gemini 2.5-Flash |
| Эмбеддинги | `intfloat/multilingual-e5-large` (1024-dim) |
| Полнотекстовый поиск | SQLite FTS5 (BM25) |
| Транскрипция аудио | Deepgram API (nova-2, ru) |
| Google Sheets отчёт | Playwright UI-автоматизация + gspread |
| Конфигурация | Python dataclass + python-dotenv |

---

## Структура файлов

```
bot/
  main.py               — точка входа: aiohttp + aiogram, регистрация команд
  config.py             — Config dataclass, читает .env
  db.py                 — все таблицы SQLite и async CRUD функции
  topic_manager.py      — обработка HDE webhook, жизненный цикл топиков
  hde_webhook.py        — aiohttp endpoint /webhook/hde
  hde_api.py            — HDE REST API client (Basic Auth)
  ai_summary.py         — Gemini AI-подсказки, детекция оборудования
  client_media.py       — скачивание вложений из HDE
  formatter.py          — шаблоны сообщений Telegram
  operator_replies.py   — обработка /note, /send, /delete
  refresh.py            — сверка топиков с HDE
  scheduler.py          — SLA-таймер, delayed delete, digest, отчёт
  general_channel.py    — необработанные тикеты в general-топике
  digest.py             — утренний дайджест
  work_schedule.py      — рабочие часы / режим отпуска
  time_utils.py         — конвертация таймзон
  tg_session.py         — retry-wrapper для Telegram API
  handlers/
    commands.py         — 18 bot-команд (/note, /send, /aianalyze, ...)
    ai_feedback.py      — callback-хендлеры кнопок AI (👍/✏️/👎/📤/💬)
  knowledge/
    indexer.py          — embed_text(), get_rag_context()
    store.py            — find_similar(): cosine + BM25 + RRF + company-буст
  reporting/
    hde_playwright.py   — UI-автоматизация скриншота из HDE
    google_sheets.py    — запись строки в Google Sheets
    runner.py           — оркестрация ежедневного отчёта
  wiki/
    searcher.py         — поиск по wiki-статьям (опционально)

tests/
  test_ai_answer_quality.py   — тесты AI компонентов
  test_topic_manager.py       — тесты жизненного цикла топиков
  ...

deploy/
  systemd/hde-bot.service     — systemd unit
  nginx/hde-bot.conf          — nginx reverse proxy

docs/
  ARCHITECTURE.md             — этот файл
  superpowers/specs/          — design specs фич
  superpowers/plans/          — implementation plans
```

---

## Базы данных (SQLite)

Файл: `hde_bot.db`

| Таблица | Назначение |
|---|---|
| `ticket_topics` | Маппинг тикет↔топик, статус, приоритет, метаданные |
| `processed_events` | Дедупликация webhook-событий |
| `reply_drafts` | Черновики ответов оператора (по topic_id) |
| `topic_media_cache` | Скачанные вложения (file_id, content_type) |
| `sent_hde_messages` | Telegram message → HDE post/комментарий |
| `unassigned_general_messages` | Сообщения необработанных тикетов в general-топике |
| `report_runs` | Дата последнего отчёта |
| `bot_settings` | Key-value: режим тишины, последний refresh и т.д. |
| `knowledge_items` | Решённые тикеты: контент, эмбеддинг, качество, company_id |
| `knowledge_fts` | FTS5 виртуальная таблица для BM25-поиска |
| `ai_feedback_pending` | Ожидающий фидбек на AI-подсказку (expires_at) |
| `solution_patterns` | Паттерны решений по оборудованию (из /aianalyze) |

---

## Переменные окружения (`.env`)

### Telegram
```
BOT_TOKEN              — токен бота
GROUP_CHAT_ID          — ID Telegram-группы с топиками (отрицательный)
PERSONAL_CHAT_ID       — Telegram ID оператора для личных уведомлений
OPERATOR_TELEGRAM_USER_IDS — ID операторов через запятую
```

### HDE
```
HDE_API_BASE_URL       — https://company.helpdeskeddy.com/api/v2
HDE_API_EMAIL          — email для Basic Auth
HDE_API_KEY            — API key для Basic Auth
HDE_WEBHOOK_SECRET     — секрет для проверки HMAC подписи webhook
HDE_OWNER_ID           — обрабатывать только тикеты этого сотрудника
HDE_OWNER_NAME         — альтернатива HDE_OWNER_ID (имя)
HDE_PUBLIC_REPLY_ENABLED        — разрешить /send (true/false)
HDE_PUBLIC_REPLY_TICKET_ALLOWLIST — CSV тикетов для /send
```

### Сервер
```
WEBHOOK_HOST           — публичный URL (https://domain.com)
WEBHOOK_PATH_TG        — путь Telegram webhook (не используется при polling)
WEBHOOK_PATH_HDE       — путь HDE webhook (/webhook/hde)
APP_PORT               — порт HTTP-сервера (default: 8080)
NGROK_STATIC_URL       — статический ngrok URL для разработки
```

### Таймеры и расписание
```
DEFAULT_REPLY_SLA_MINUTES    — SLA время ответа (default: 30)
PRE_SLA_WARNING_MINUTES      — за сколько минут предупреждать (default: 10)
SCHEDULER_INTERVAL_SECONDS   — интервал проверки планировщика (default: 30)
WORK_DAYS                    — рабочие дни: 0,1,2,3,6 = Пн-Чт+Вс
WORK_HOUR_START              — начало рабочего дня (Moscow, default: 9)
WORK_HOUR_END                — конец рабочего дня (Moscow, default: 18)
REPORT_SEND_HOUR_UTC         — час UTC для автоотчёта (default: 6 = 9:00 МСК)
GENERAL_TOPIC_ID             — ID топика для необработанных тикетов
UNASSIGNED_DEPARTMENT        — фильтр отдела для необработанных тикетов
```

### AI
```
GEMINI_API_KEY         — Google Gemini API key (aistudio.google.com)
DEEPGRAM_API_KEY       — Deepgram key для транскрипции аудио (console.deepgram.com)
```

### Google Sheets отчёт
```
HDE_REPORT_PASSWORD            — пароль для HDE web UI
GOOGLE_SERVICE_ACCOUNT_FILE    — путь к JSON-ключу сервисного аккаунта
GOOGLE_SPREADSHEET_ID          — ID таблицы из URL Google Sheets
GOOGLE_WORKSHEET_NAME          — название листа
GOOGLE_SHEET_NAME_IN_A         — значение в колонке A (имя оператора)
```

---

## Основные потоки данных

### 1. Новый тикет (HDE → Telegram)

```
HDE отправляет webhook (event: assigned_on_create)
  → hde_webhook.py: проверка HMAC, парсинг payload
  → topic_manager.py: handle_new_ticket()
    → db.get_topic() — проверить нет ли уже топика
    → bot.create_forum_topic() — создать топик с цветом по приоритету
    → db.save_topic() — сохранить маппинг ticket_id ↔ topic_id
    → formatter.format_new_ticket() — сформировать сообщение
    → bot.send_message() — в топик
    → generate_ticket_summary() — AI-подсказка (если включено)
      → _detect_equipment() — определить бренд (АТОЛ/Эвотор/...)
      → get_rag_context() → find_similar() — поиск похожих случаев
      → find_solution_pattern() — типовые шаги из solution_patterns
      → Gemini API — генерация подсказки
      → bot.send_message() — "🧠 Суть (78%): ..." + "💡 Предложенный ответ: ..."
      → db.save_feedback_pending() — сохранить для кнопок 👍/👎
```

### 2. Ответ клиента (HDE → Telegram)

```
HDE webhook: event = client_reply
  → topic_manager.py: handle_client_reply()
    → db.get_topic() — найти топик по ticket_id
    → client_media.py: скачать вложения из HDE
      → Deepgram: транскрипция аудио (если есть голосовые)
    → formatter.format_client_reply() — с вложениями
    → bot.send_message() / send_photo() / send_document()
```

### 3. Ответ оператора (Telegram → HDE)

```
Оператор пишет /send текст (или reply на сообщение)
  → commands.py: cmd_send()
    → operator_replies.py: process_send()
      → hde_api.py: add_post(ticket_id, text, attachments)
      → db.save_sent_hde_message() — для /delete
      → Telegram reaction ✅
```

### 4. AI кнопка 📤 Ответить клиенту

```
Оператор нажимает "📤 Ответить клиенту" на AI-подсказке
  → ai_feedback.py: cb_send_to_hde()
    → db.get_feedback_pending() — достать текст ответа
    → hde_api.add_post(ticket_id, answer_text) — отправить в HDE
    → message.edit_text("✅ Отправлено клиенту") — убрать кнопки
```

### 5. Implicit feedback (обучение)

```
Оператор пишет ответ в HDE топике
  → topic_manager.py: handle_staff_reply()
    → _implicit_feedback()
      → db.get_feedback_pending() — найти AI-подсказку
      → difflib.SequenceMatcher — сравнить ответ оператора с AI
      → ratio >= 0.7: сохранить как implicit_good → пополнить базу знаний
      → ratio >= 0.85: _maybe_update_pattern() — усилить/создать паттерн
      → ratio <= 0.35: сохранить как implicit_corrected → обновить wiki
```

### 6. /aianalyze — создание базы паттернов

```
Оператор пишет /aianalyze
  → commands.py: cmd_aianalyze()
    → db: загрузить knowledge_items (source: hde_closed, feedback, implicit_good)
    → батчи по 15 тикетов → Gemini:
       "Извлеки equipment, problem_type, steps из каждого тикета"
    → Gemini возвращает JSON
    → pattern_exists_similar() — проверить на дубликат (difflib >= 0.7)
    → save_solution_pattern() — сохранить новые паттерны
    → итоговый отчёт в Telegram
```

---

## AI-компоненты подробно

### Детекция оборудования

`_detect_equipment(title, history)` — regex по заголовку тикета + первые 300 символов переписки:

| Паттерн | Бренд |
|---|---|
| `\bатол\b`, `atol`, `\bфр\b` | АТОЛ |
| `\bэвотор\b`, `evotor` | Эвотор |
| `\bштрих\b`, `shtrih` | Штрих-М |
| `\bviki\b`, `вики` | Viki |
| `\bсбер\b`, `sber`, `сбербанк` | Эквайринг Сбер |
| `\bвтб\b`, `vtb` | Эквайринг ВТБ |
| `тинькофф`, `tinkoff` | Эквайринг Тинькофф |
| `\bптк\b`, `ptkf` | ПТК |

### RAG (Retrieval-Augmented Generation)

Поиск похожих случаев перед генерацией подсказки:

1. `embed_text(query)` → вектор 1024-dim через `multilingual-e5-large`
2. `find_similar(embedding, company_id=...)`:
   - Cosine similarity по всем `knowledge_items` (embedding IS NOT NULL)
   - BM25 через FTS5 для точного поиска по словам (ошибки, коды, бренды)
   - Слияние через RRF (k=60)
   - Company boost: `+1/(k+1)` для тикетов той же компании
   - Возвращает `list[tuple[KnowledgeItem, float]]`
3. `confidence_pct = int(max_cosine_score * 100)`

Confidence score показывается в сообщении если >= 40%:
```
🧠 Суть (78%): АТОЛ 30Ф — ошибка связи с ОФД
```

### System prompt (специализация)

Промпт заточен под специалиста 2-й линии по кассовому оборудованию:
- Роль: "подсказываю СПЕЦИАЛИСТУ, не пишу ответ клиенту"
- Добавляет блок с брендом оборудования (если определён)
- Добавляет типовые шаги решения из `solution_patterns` (если есть)
- Добавляет похожие случаи из knowledge base (RAG)
- Формат ответа: `Суть:` + `Ответ:` через `→`

### solution_patterns — база паттернов

Таблица типовых решений по оборудованию. Пополняется двумя способами:

1. **Команда `/aianalyze`** — анализирует все решённые тикеты через Gemini, извлекает паттерны оптом
2. **Implicit feedback** — когда оператор пишет ответ похожий на AI (ratio >= 0.85), `_maybe_update_pattern` либо усиливает существующий паттерн (`use_count++`), либо создаёт новый через Gemini

---

## Команды бота (полный список)

| Команда | Что делает |
|---|---|
| `/start` | Приветствие |
| `/status` | Активные топики, pending delete, pre-SLA счётчики |
| `/help` | Список команд |
| `/note текст` | Внутренний комментарий в HDE |
| `/send текст` | Публичный ответ клиенту через HDE |
| `/delete` | Удалить сообщение оператора из HDE (reply на него) |
| `/refresh` | Сверить локальные топики с HDE (восстановить пропущенные) |
| `/report` | Отчёт за вчера → Google Sheets |
| `/report YYYY-MM-DD` | Отчёт за конкретную дату |
| `/vacation Nd` | Режим тишины на N дней |
| `/workon` | Выйти из режима тишины |
| `/aisummary` | Сгенерировать AI-подсказку вручную |
| `/aiknowledge запрос` | Найти похожие случаи в базе знаний |
| `/aistatus` | Статистика базы знаний (счётчики, размер эмбеддингов) |
| `/aiimport` | Импортировать решённые тикеты из HDE в базу знаний |
| `/aibackfill` | Переиндексировать прошлые тикеты |
| `/aireindex` | Пересчитать все эмбеддинги |
| `/aianalyze` | Извлечь паттерны решений из базы знаний через Gemini |
| `/digest` | Сгенерировать утренний дайджест |

---

## Деплой

### VPS (production)

```bash
git pull
systemctl restart hde-bot
```

Миграции БД применяются автоматически при старте (`CREATE TABLE IF NOT EXISTS`).

### Локальная разработка

```bash
python start_dev.py   # поднимает ngrok + запускает бота
```

### systemd

```ini
# deploy/systemd/hde-bot.service
[Service]
WorkingDirectory=/home/user/HDE_bot
ExecStart=/home/user/HDE_bot/.venv/bin/python -m bot.main
Restart=always
```

### nginx (reverse proxy)

Проксирует `/webhook/hde` на `localhost:8080`.

---

## Тесты

```bash
pytest tests/ -q
```

Основные наборы:
- `tests/test_ai_answer_quality.py` — solution_patterns CRUD, детекция оборудования, RAG, confidence score
- `tests/test_topic_manager.py` — жизненный цикл топиков

---

## Что планируется дальше

- **Knowledge Management** — дедупликация базы, expiry, `/aimetrics`
- **Quick Replies** — кнопки с шаблонными ответами
- **HDE Status Updates** — смена статуса/приоритета тикета из Telegram
