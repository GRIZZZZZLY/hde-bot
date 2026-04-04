# HDE Telegram Topic Bot

Telegram-бот для интеграции с HelpDeskEddy: создает и ведет forum topics по тикетам, считает pre-SLA напоминания, пересылает клиентские ответы в topic и позволяет сотруднику отвечать из Telegram через HDE API.

## Что делает

- Создает topic, когда тикет назначается на нужного сотрудника.
- Переименовывает topic, если меняется тема тикета.
- Закрывает topic при снятии с сотрудника и удаляет его через 8 часов.
- Удаляет topic сразу при закрытии тикета.
- Публикует ответы клиента в topic.
- Пересылает клиентские вложения в topic: фото, видео, voice, audio, документы.
- Считает точные pre-SLA напоминания внутри бота и отправляет их в личные сообщения.
- Позволяет сотруднику писать внутренние комментарии и публичные ответы из Telegram:
  - `/note`
  - `/send`
- Поддерживает отправку текста, одиночных вложений и альбомов из Telegram в HDE.

## Как это устроено

```text
HelpDeskEddy -> webhook -> bot -> Telegram topics / personal alerts
Telegram operator commands -> bot -> HDE API
```

Проект работает как webhook-приложение на `aiohttp + aiogram`, хранит состояние в `SQLite` и поднимает фоновый scheduler для pre-SLA и отложенного удаления topics.

## Быстрый старт

### 1. Установить зависимости

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
```

### 2. Подготовить `.env`

Скопируйте [`.env.example`](.env.example) в `.env` и заполните реальные значения.

Ключевые переменные:

- `BOT_TOKEN`
- `GROUP_CHAT_ID`
- `PERSONAL_CHAT_ID`
- `HDE_WEBHOOK_SECRET`
- `HDE_OWNER_ID`
- `HDE_API_BASE_URL`
- `HDE_API_EMAIL`
- `HDE_API_KEY`
- `WEBHOOK_HOST`

### 3. Локальный запуск

Для локальной разработки:

```bash
python start_dev.py
```

Скрипт поднимет `ngrok`, обновит `WEBHOOK_HOST` в `.env` и запустит бота.

Для фиксированного `ngrok`-домена можно использовать:

```env
NGROK_STATIC_URL=https://your-static-domain.ngrok-free.dev
```

### 4. Ручной запуск без `ngrok`

```bash
python -m bot.main
```

## Команды бота

- `/start` — краткое описание
- `/status` — число активных topics, pending delete и pre-SLA
- `/help` — список команд
- `/note текст` — внутренний комментарий в HDE
- `/note` reply-ем на сообщение/медиа — внутренний комментарий с текстом, файлом или альбомом
- `/send текст` — публичный ответ клиенту через HDE
- `/send` reply-ем на сообщение/медиа — ответ клиенту с текстом, файлом или альбомом

## События HDE

Бот ожидает webhook-события:

- `assigned_on_create`
- `owner_changed`
- `ticket_updated`
- `client_reply`
- `staff_reply`
- `ticket_closed`

Подробная логика и правила HDE описаны в [`docs/HDE_TELEGRAM_HYBRID_SPEC.md`](docs/HDE_TELEGRAM_HYBRID_SPEC.md).

## Структура проекта

```text
bot/
  main.py               # aiohttp + aiogram entrypoint
  hde_webhook.py        # прием webhook от HDE
  topic_manager.py      # lifecycle topics
  scheduler.py          # pre-SLA и delayed delete
  operator_replies.py   # /note и /send
  hde_api.py            # HDE API client
  client_media.py       # клиентские вложения из HDE webhook
  db.py                 # SQLite storage
  formatter.py          # тексты и визуальный стиль сообщений
deploy/
  systemd/
  nginx/
docs/
  HDE_TELEGRAM_HYBRID_SPEC.md
tests/
```

## Тесты

```bash
pytest -q
```

## Деплой на VPS

На VPS нужны только:

- `bot/`
- `requirements.txt`
- `.env`
- `hde_bot.db` — только если нужно перенести текущее состояние

Не нужны:

- `tests/`
- `start_dev.py`
- локальные кэши и `__pycache__`

Готовые шаблоны:

- [`deploy/systemd/hde-bot.service`](deploy/systemd/hde-bot.service)
- [`deploy/nginx/hde-bot.conf`](deploy/nginx/hde-bot.conf)

На VPS замените `WEBHOOK_HOST` на постоянный HTTPS-домен и обновите URL webhook в HDE.

## Что важно знать

- `.env` не должен попадать в репозиторий.
- SQLite-файл хранит состояние topics и scheduler.
- Для operator replies нужен корректный `HDE_API_EMAIL + HDE_API_KEY`.
- Для входящих клиентских вложений правило `client_reply` в HDE должно передавать ссылки на вложения.
- Для публичных ответов из Telegram нужно тестировать фактическую доставку в клиентский канал HDE.

## Статус проекта

Проект находится на стадии рабочего интеграционного прототипа:

- core-логика реализована;
- локальный запуск и VPS-деплой предусмотрены;
- тесты есть;
- эксплуатационные артефакты минимальны и лежат в `deploy/`.
