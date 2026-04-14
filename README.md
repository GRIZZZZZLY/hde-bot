# HDE Telegram Topic Bot

Telegram-бот для специалиста 2-й линии поддержки кассового оборудования. Мост между HelpDeskEddy (HDE) и Telegram: каждый тикет — отдельный forum topic, клиентские сообщения появляются автоматически, AI-подсказка генерируется при каждом новом тикете.

> Полная архитектура и описание всех компонентов: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Что умеет

**Работа с тикетами**
- Создаёт Telegram topic при назначении тикета, цвет зависит от приоритета
- Публикует ответы клиента и вложения (фото, видео, голосовые, документы)
- Транскрибирует аудиосообщения клиента через Deepgram
- Закрывает topic при снятии тикета, удаляет через 8 часов
- Синхронизирует состояние с HDE по команде `/refresh`

**Ответы из Telegram**
- `/send` — публичный ответ клиенту через HDE API
- `/note` — внутренний комментарий в HDE
- `/delete` — удалить отправленное сообщение из HDE
- Поддержка текста, вложений и альбомов в /send и /note

**AI-подсказки**
- При каждом новом тикете три тихих сообщения (без уведомлений):
  - `🧠 Суть (78%)` — диагноз и бренд оборудования
  - `💬 Ответ клиенту` — готовый ответ/вопрос/инструкция для клиента
  - `📋 Памятка` — чеклист и шаги решения для специалиста
- Основной провайдер: Groq (llama-3.3-70b), Gemini как fallback
- Ищет похожие решённые случаи (RAG: cosine + BM25 + RRF)
- Подсказывает типовые шаги из базы паттернов
- Внутренние комментарии коллег (1я линия) включены в контекст
- Кнопки прямой отправки в HDE: `📤 Ответить клиенту` / `💬 Комментарий`
- Учится на ответах оператора (implicit feedback)
- Ночной prompt optimizer: каждую ночь проверяет можно ли улучшить промпт

**Прочее**
- SLA-таймер: личное уведомление за 10 минут до истечения
- Утренний дайджест
- Ежедневный отчёт в Google Sheets (через Playwright + gspread)
- Режим отпуска: `/vacation 3d`

---

## Быстрый старт

### 1. Установить зависимости

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/Mac
pip install -U pip
pip install -r requirements.txt
```

Для ежедневного отчёта (опционально):

```bash
python -m playwright install --with-deps chromium
```

### 2. Настроить `.env`

```bash
cp .env.example .env
```

Заполнить обязательные переменные:

```env
# Telegram
BOT_TOKEN=
GROUP_CHAT_ID=          # ID группы с топиками (отрицательный)
PERSONAL_CHAT_ID=       # Telegram ID оператора

# HDE
HDE_API_BASE_URL=       # https://company.helpdeskeddy.com/api/v2
HDE_API_EMAIL=
HDE_API_KEY=
HDE_WEBHOOK_SECRET=     # любая случайная строка, совпадает с настройкой в HDE
HDE_OWNER_ID=           # ID сотрудника в HDE (чьи тикеты обрабатывать)

# Сервер
WEBHOOK_HOST=           # https://your-domain.com

# AI (опционально, но рекомендуется)
GROQ_API_KEY=           # https://console.groq.com (основной AI провайдер)
GEMINI_API_KEY=         # https://aistudio.google.com/apikey (fallback)
DEEPGRAM_API_KEY=       # https://console.deepgram.com (транскрипция аудио)
```

### 3. Локальный запуск

```bash
python start_dev.py     # поднимает ngrok и запускает бота
```

Или без ngrok (если уже есть публичный URL):

```bash
python -m bot.main
```

---

## Команды бота

| Команда | Описание |
|---|---|
| `/status` | Активные топики, pre-SLA счётчики |
| `/help` | Список команд |
| `/note текст` | Внутренний комментарий в HDE |
| `/send текст` | Публичный ответ клиенту |
| `/delete` | Удалить сообщение из HDE (reply) |
| `/refresh` | Синхронизировать топики с HDE |
| `/report` | Записать отчёт в Google Sheets |
| `/vacation Nd` | Режим тишины на N дней |
| `/workon` | Выйти из режима тишины |
| `/aianalyze` | Извлечь паттерны решений из базы знаний |
| `/aiimport` | Импортировать тикеты из HDE в базу знаний |
| `/aistatus` | Статистика AI и базы знаний |
| `/aiknowledge запрос` | Поиск по базе знаний |
| `/aireindex` | Пересчитать эмбеддинги |
| `/aioptimize` | Запустить ночной оптимизатор промпта вручную |

---

## Деплой на VPS

### Первичный деплой

```bash
git clone ... && cd HDE_bot
python -m venv .venv && pip install -r requirements.txt
cp .env.example .env   # заполнить переменные
cp deploy/systemd/hde-bot.service /etc/systemd/system/
systemctl enable hde-bot && systemctl start hde-bot
```

### Обновление

```bash
git pull && systemctl restart hde-bot
```

Миграции базы данных применяются автоматически при каждом старте.

### Первоначальная настройка AI

После первого запуска:

```
/aiimport      — импортировать решённые тикеты из HDE
/aianalyze     — извлечь паттерны решений (занимает 3-5 минут)
```

После этого AI-подсказки будут использовать реальную базу знаний.

### nginx

```nginx
location /webhook/hde {
    proxy_pass http://127.0.0.1:8080;
}
```

Готовый конфиг: [deploy/nginx/hde-bot.conf](deploy/nginx/hde-bot.conf)

---

## Структура проекта

```
bot/
  main.py               — точка входа
  config.py             — конфигурация из .env
  db.py                 — SQLite: схема и все CRUD функции
  topic_manager.py      — обработка webhook, жизненный цикл топиков
  hde_api.py            — HDE REST API client
  ai_summary.py         — AI-подсказки (Groq primary, Gemini fallback)
  scheduler.py          — SLA, delayed delete, digest, optimizer trigger
  operator_replies.py   — /note, /send, /delete
  handlers/
    commands.py         — все команды бота
    ai_feedback.py      — кнопки 👍/✏️/👎/📤/💬
  knowledge/
    indexer.py          — векторные эмбеддинги
    store.py            — поиск: cosine + BM25 + RRF
    wiki/
      builder.py        — построение wiki из паттернов
  optimizer/
    agent.py            — главный цикл оптимизации
    evaluator.py        — оценка кандидатов на датасете
    mutations.py        — промпты для генерации мутаций
    llm_router.py       — multi-LLM роутер (Gemini, Groq)
  reporting/
    runner.py           — ежедневный отчёт

tests/
deploy/
docs/
  ARCHITECTURE.md       — полная архитектура
```

---

## Тесты

```bash
pytest tests/ -q
```

---

## Безопасность

- `.env` и `secrets/` не попадают в репозиторий (`.gitignore`)
- HDE webhook верифицируется по HMAC-подписи
- SQLite хранится локально, не экспортируется
- Для Google Sheets используется service account (не OAuth)

---

## Что планируется

- **Knowledge Management** — дедупликация, expiry, `/aimetrics`
- **Quick Replies** — кнопки с шаблонами ответов
- **HDE Status Updates** — смена статуса тикета из Telegram
- **AI Speed** — убрать задержку Gemini 429, переключить генерацию на Groq
