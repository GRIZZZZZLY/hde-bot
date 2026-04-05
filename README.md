# HDE Telegram Topic Bot

Telegram-бот для интеграции с HelpDeskEddy. Бот ведет forum topics по тикетам, считает pre-SLA, пересылает клиентские ответы и позволяет сотруднику отвечать из Telegram через HDE API.

## Что умеет

- Создает topic, когда тикет назначается на нужного сотрудника.
- Переименовывает topic, если меняется тема тикета.
- Закрывает topic при снятии с сотрудника и удаляет его через 8 часов.
- Удаляет topic сразу при закрытии тикета.
- Публикует ответы клиента в topic.
- Пересылает клиентские вложения в topic: фото, видео, voice, audio, документы.
- Отправляет pre-SLA напоминания в личные сообщения.
- Поддерживает команды оператора:
  - `/note`
  - `/send`
  - `/delete`
  - `/refresh`
  - `/report`
- Поддерживает отправку текста, одиночных вложений и альбомов из Telegram в HDE.
- Поддерживает утренний digest и опциональный ежедневный отчет в Google Sheets.

## Архитектура

```text
HelpDeskEddy -> webhook -> bot -> Telegram topics / personal alerts
Telegram operator commands -> bot -> HDE API
Scheduler -> pre-SLA / delayed delete / digest / daily report
```

Telegram обновления принимаются через polling. HDE обновления приходят в отдельный `aiohttp` webhook endpoint.

## Быстрый старт

### 1. Установить зависимости

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
```

Если планируете запускать ежедневный отчет через Playwright:

```bash
python -m playwright install chromium
```

На Linux/VPS обычно лучше:

```bash
python -m playwright install --with-deps chromium
```

### 2. Подготовить `.env`

Скопируйте [`.env.example`](.env.example) в `.env` и заполните реальные значения.

Ключевые переменные для основного бота:

- `BOT_TOKEN`
- `GROUP_CHAT_ID`
- `PERSONAL_CHAT_ID`
- `HDE_WEBHOOK_SECRET`
- `HDE_OWNER_ID` или `HDE_OWNER_NAME`
- `HDE_API_BASE_URL`
- `HDE_API_EMAIL`
- `HDE_API_KEY`
- `WEBHOOK_HOST`

Дополнительно для ежедневного отчета:

- `HDE_REPORT_PASSWORD` или `HDE_API_KEY`
- `GOOGLE_SERVICE_ACCOUNT_FILE`
- `GOOGLE_SPREADSHEET_ID`
- `GOOGLE_WORKSHEET_NAME`
- `GOOGLE_SHEET_NAME_IN_A`

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

### 4. Запуск без `ngrok`

```bash
python -m bot.main
```

## Команды бота

- `/start` — краткое описание
- `/status` — число активных topics, pending delete и pre-SLA
- `/help` — список команд
- `/note текст` — внутренний комментарий в HDE
- `/note` reply-ем на сообщение или медиа — комментарий с вложением или альбомом
- `/send текст` — публичный ответ клиенту через HDE
- `/send` reply-ем на сообщение или медиа — ответ с текстом, caption и вложениями
- `/delete` reply-ем на отправленное оператором сообщение — удалить его из HDE
- `/refresh` — синхронизировать локальные topics с HDE
- `/report` — записать отчет по операторам в Google Sheets за вчера
- `/report YYYY-MM-DD` — отчет за конкретную дату

## События HDE

Бот ожидает webhook-события:

- `assigned_on_create`
- `owner_changed`
- `ticket_updated`
- `client_reply`
- `staff_reply`
- `ticket_closed`

Подробная логика и правила HDE описаны в [docs/HDE_TELEGRAM_HYBRID_SPEC.md](docs/HDE_TELEGRAM_HYBRID_SPEC.md).

## Структура проекта

```text
bot/
  main.py               # aiohttp + aiogram entrypoint
  hde_webhook.py        # прием webhook от HDE
  topic_manager.py      # lifecycle topics
  scheduler.py          # pre-SLA, delayed delete, digest, report
  operator_replies.py   # /note, /send, /delete
  hde_api.py            # HDE API client
  client_media.py       # клиентские вложения из HDE webhook
  refresh.py            # сверка локальных topics с HDE
  reporting/
    hde_playwright.py   # UI-автоматизация отчета HDE
    google_sheets.py    # запись в Google Sheets
    runner.py           # orchestration пайплайна отчета
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

Минимально для основного бота нужны:

- `bot/`
- `requirements.txt`
- `.env`
- `hde_bot.db` — только если нужно перенести текущее состояние

Если включаете ежедневный отчет, дополнительно нужны:

- `GOOGLE_SERVICE_ACCOUNT_FILE`, указанный в `.env`
- установленный Chromium для Playwright

На VPS не нужны:

- `tests/`
- `start_dev.py`
- локальные кэши и `__pycache__`

Готовые шаблоны:

- [deploy/systemd/hde-bot.service](deploy/systemd/hde-bot.service)
- [deploy/nginx/hde-bot.conf](deploy/nginx/hde-bot.conf)

На VPS замените `WEBHOOK_HOST` на постоянный HTTPS-домен и обновите URL webhook в HDE.

## Что важно знать

- `.env` и `secrets/` не должны попадать в репозиторий.
- SQLite хранит состояние topics, scheduler и сервисные маппинги.
- Для operator replies нужен корректный `HDE_API_EMAIL + HDE_API_KEY`.
- Для входящих клиентских вложений правило `client_reply` в HDE должно передавать ссылки на вложения.
- Для публичных ответов из Telegram нужно тестировать фактическую доставку в клиентский канал HDE.
- Ежедневный отчет опционален. Если переменные report-модуля не заполнены, основной бот продолжит работать без него.
- Для Google Sheets используется service account. Нужно расшарить таблицу на email этого service account.

## Статус проекта

Проект находится на стадии рабочего интеграционного прототипа:

- основная HDE/Telegram логика реализована;
- локальный запуск и VPS-деплой предусмотрены;
- тесты есть;
- reporting-модуль можно запускать на VPS без интерактивного Google OAuth.
