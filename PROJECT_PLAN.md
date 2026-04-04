# HDE Notification Router Bot — Технический план

## Цель проекта

Telegram-бот, который принимает вебхуки от HelpDeskEddy и маршрутизирует уведомления
в топики (Forum Topics) Telegram-группы. Разделяет шум на потоки, пушит SLA-алерты
в личку, автоматически управляет жизненным циклом топиков.

---

## Стек

| Компонент          | Технология            | Почему                                                    |
|--------------------|-----------------------|-----------------------------------------------------------|
| Язык               | Python 3.12+          | Основной язык, опыт с aiogram                             |
| Telegram Bot       | aiogram 3.x           | Нативная поддержка Forum Topics API, async, webhook-режим |
| HTTP-сервер        | aiohttp               | Встроен в aiogram, обслуживает и TG-вебхуки, и HDE-вебхуки на одном порте |
| База данных        | SQLite + aiosqlite    | Лёгкая, без инфраструктуры, хранит маппинг ticket→topic   |
| Конфигурация       | python-dotenv + .env  | Токены и ID не в коде                                     |
| Деплой             | VPS (Ubuntu) или Docker | Нужен публичный URL для приёма вебхуков от HDE            |
| Reverse proxy      | Caddy или nginx       | HTTPS-терминация (HDE шлёт вебхуки только на HTTPS)       |

### Зависимости (requirements.txt)

```
aiogram>=3.10,<4.0
aiohttp>=3.9
aiosqlite>=0.19
python-dotenv>=1.0
```

---

## Структура проекта

```
C:\HDE_bot\
├── .env                    # Секреты (не в git)
├── .env.example            # Шаблон переменных
├── .gitignore
├── requirements.txt
├── PROJECT_PLAN.md         # Этот файл
├── bot/
│   ├── __init__.py
│   ├── main.py             # Точка входа: запуск aiogram + aiohttp
│   ├── config.py           # Pydantic Settings или dataclass из .env
│   ├── db.py               # SQLite: init, CRUD для маппинга ticket↔topic
│   ├── hde_webhook.py      # aiohttp handler: POST /hde — парсит JSON от HDE
│   ├── topic_manager.py    # Логика: create/close/reopen/delete топиков
│   ├── formatter.py        # Форматирование сообщений для Telegram (HTML)
│   └── handlers/
│       └── commands.py     # /start, /status, /help — команды в ЛС бота
└── tests/
    ├── test_hde_payload.py # Тест парсинга JSON от HDE
    └── test_topic_manager.py
```

---

## Переменные окружения (.env)

```env
# Telegram
BOT_TOKEN=123456:ABC-DEF...
GROUP_CHAT_ID=-100XXXXXXXXXX       # ID группы с топиками
PERSONAL_CHAT_ID=123456789          # Твой личный TG ID для SLA-алертов

# HDE
HDE_WEBHOOK_SECRET=random_secret_string   # Проверка подлинности вебхука (опционально)

# Сервер
WEBHOOK_HOST=https://your-domain.com
WEBHOOK_PATH_TG=/webhook/telegram
WEBHOOK_PATH_HDE=/webhook/hde
APP_PORT=8080
```

---

## Схема работы

### 1. Приём вебхуков

Бот запускает один aiohttp-сервер на порту 8080 с двумя маршрутами:

- `POST /webhook/telegram` — стандартный вебхук aiogram от Telegram
- `POST /webhook/hde` — вебхук от HelpDeskEddy

### 2. Формат JSON от HDE

В Диспетчере HDE настраиваются 3 правила. Каждое отправляет POST JSON
на `https://your-domain.com/webhook/hde`. Тело формируется из тегов HDE:

**Правило 1 — Новый ответ клиента:**
```
Обязательное условие: Новый ответ в заявке
Дополнительные:
  - Исполнитель заявки = Игорь Кравцов
  - Группа автора последнего ответа = Клиент
```

```json
{
  "event_type": "client_reply",
  "ticket_id": "{unique_id}",
  "ticket_name": "$strip_tags({ticket_name})",
  "company_name": "{company_name}",
  "user_name": "{user_name}",
  "priority": "{priority}",
  "status": "{status}",
  "sla_remaining": "{sla_remaining_minutes}",
  "message": "$strip_tags({answer_last_without_html})",
  "link": "{link_staff}",
  "secret": "random_secret_string"
}
```

**Правило 2 — SLA горит:**
```
Обязательное условие: Достигнут SLA
Дополнительные:
  - Исполнитель заявки = Игорь Кравцов
  - Статус заявки ≠ Закрыта
```

```json
{
  "event_type": "sla_alert",
  "ticket_id": "{unique_id}",
  "ticket_name": "$strip_tags({ticket_name})",
  "company_name": "{company_name}",
  "user_name": "{user_name}",
  "priority": "{priority}",
  "sla_remaining": "{sla_remaining_minutes}",
  "link": "{link_staff}",
  "secret": "random_secret_string"
}
```

**Правило 3 — Заявка закрыта:**
```
Обязательное условие: Изменения в заявке
Дополнительные:
  - Статус заявки = Закрыта (или ваш финальный статус)
  - Исполнитель заявки = Игорь Кравцов
```

```json
{
  "event_type": "ticket_closed",
  "ticket_id": "{unique_id}",
  "ticket_name": "$strip_tags({ticket_name})",
  "company_name": "{company_name}",
  "link": "{link_staff}",
  "secret": "random_secret_string"
}
```

### 3. Логика обработки (topic_manager.py)

```
event_type == "client_reply":
  1. Ищем в SQLite: есть ли topic_id для данного ticket_id?
  2. НЕТ → bot.create_forum_topic(chat_id=GROUP, name="#{ticket_id} — {company} — {ticket_name}")
        → сохраняем ticket_id ↔ topic_id в SQLite
        → отправляем сообщение в новый топик
  3. ДА  → проверяем, закрыт ли топик (поле is_closed в БД)
        → если закрыт → bot.reopen_forum_topic() → обновляем БД
        → отправляем сообщение в существующий топик

event_type == "sla_alert":
  1. Отправляем сообщение в ЛИЧКУ (PERSONAL_CHAT_ID) — это единственный громкий канал
  2. Если есть топик для этого тикета — дублируем туда с пометкой ⚠️

event_type == "ticket_closed":
  1. Ищем topic_id для ticket_id в SQLite
  2. Если найден → отправляем финальное сообщение "✅ Заявка закрыта"
                 → bot.close_forum_topic()
                 → обновляем is_closed = true, closed_at = now() в БД
  3. Если не найден → игнорируем (тикет мог быть без ответов клиента)
```

### 4. База данных (db.py)

Одна таблица:

```sql
CREATE TABLE IF NOT EXISTS ticket_topics (
    ticket_id   TEXT PRIMARY KEY,       -- unique_id из HDE, напр. "ABC-123"
    topic_id    INTEGER NOT NULL,       -- message_thread_id из Telegram
    company     TEXT DEFAULT '',
    ticket_name TEXT DEFAULT '',
    is_closed   INTEGER DEFAULT 0,      -- 0 = открыт, 1 = закрыт
    created_at  TEXT DEFAULT (datetime('now')),
    closed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_closed ON ticket_topics(is_closed, closed_at);
```

### 5. Формат сообщений (formatter.py)

**Новый ответ клиента (в топик группы):**
```
👤 Ivan Krasnov
💬 Касса не передаёт данные в ОФД, 5555 порт

⏱ SLA осталось: 2ч 15мин
🔗 Открыть в HDE
```

**SLA-алерт (в личку):**
```
🔴 SLA ПРОСРОЧЕН

📋 #ABC-123 — Касса не передаёт данные в ОФД
🏢 ООО Ромашка
⏱ Просрочено на: 30 мин
🔗 Открыть в HDE
```

**Заявка закрыта (в топик):**
```
✅ Заявка закрыта
```

### 6. Команды бота (handlers/commands.py)

| Команда   | Где работает | Что делает                                        |
|-----------|-------------|---------------------------------------------------|
| /start    | Личка       | Приветствие, проверка что бот работает             |
| /status   | Личка       | Показывает кол-во открытых топиков/тикетов из БД  |
| /help     | Личка       | Список команд                                     |

---

## Деплой

### Вариант A: VPS + Caddy (рекомендуемый)

```bash
# На VPS (Ubuntu 22/24)
sudo apt update && sudo apt install -y python3.12 python3.12-venv caddy

# Проект
git clone <repo> /opt/hde_bot
cd /opt/hde_bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Caddy автоматически выдаст Let's Encrypt сертификат
# /etc/caddy/Caddyfile:
# your-domain.com {
#     reverse_proxy localhost:8080
# }

# Systemd unit
sudo cp deploy/hde-bot.service /etc/systemd/system/
sudo systemctl enable --now hde-bot
```

### Вариант B: Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "bot.main"]
```

### Вариант C: ngrok (для тестирования)

```bash
# Локально на Windows
ngrok http 8080
# Скопировать https://xxxx.ngrok.io в .env как WEBHOOK_HOST
# В HDE вебхуки указывают на https://xxxx.ngrok.io/webhook/hde
```

---

## Порядок разработки (фазы)

### Фаза 1 — Скелет (MVP)
1. Инициализировать проект, venv, установить зависимости
2. `config.py` — загрузка .env
3. `db.py` — создание таблицы, базовые CRUD
4. `main.py` — запуск aiogram webhook + aiohttp с маршрутом /webhook/hde
5. `hde_webhook.py` — приём POST, парсинг JSON, валидация secret
6. `topic_manager.py` — create_topic + send_message для event_type=client_reply
7. Тест через ngrok: создать тестовое правило в HDE → убедиться что топик создаётся

### Фаза 2 — Полный цикл
1. Обработка event_type=ticket_closed → close_forum_topic
2. Обработка event_type=sla_alert → сообщение в личку
3. Reopen топика при повторном client_reply на закрытый тикет
4. `formatter.py` — красивое HTML-форматирование сообщений

### Фаза 3 — Надёжность
1. Логирование (logging) — все входящие вебхуки в лог
2. Обработка ошибок: Telegram API rate limits, невалидный JSON от HDE
3. Команды /start, /status, /help
4. Деплой на VPS + Caddy + systemd

### Фаза 4 — Улучшения (опционально)
1. Цветные иконки топиков по приоритету (icon_color в createForumTopic)
2. Утренняя сводка: cron-задача → «У тебя 5 открытых тикетов, 1 SLA горит»
3. Inline-кнопки в сообщениях (Открыть в HDE, Закрыть тикет)
4. Поддержка нескольких исполнителей (мультитенант)

---

## Ключевые моменты из документации

### Telegram Bot API — Forum Topics
- `createForumTopic(chat_id, name, icon_color?)` → возвращает `message_thread_id`
- `closeForumTopic(chat_id, message_thread_id)` → закрывает (нельзя писать)
- `reopenForumTopic(chat_id, message_thread_id)` → переоткрывает
- `deleteForumTopic(chat_id, message_thread_id)` → удаляет со всеми сообщениями
- `sendMessage(chat_id, text, message_thread_id)` → пишет в конкретный топик
- Бот должен быть **администратором** группы с правом `can_manage_topics`
- Доступные icon_color: 0x6FB9F0 (голубой), 0xFFD67E (жёлтый), 0xCB86DB (фиолетовый), 0x8EEE98 (зелёный), 0xFF93B2 (розовый), 0xFB6F5F (красный)
- Лимит топиков на группу: до 1 000 000 — ротация не нужна
- Название топика: до 128 символов

### HelpDeskEddy — Диспетчер
- Условия: Новый ответ, Новый комментарий, Изменения, Достигнут SLA, Минут от последнего ответа
- Доп. условия: Исполнитель, Приоритет, Статус, Группа автора ответа, Компания
- Действие: Отправить вебхук (POST/JSON, синхронный/асинхронный)
- Теги в теле вебхука: {unique_id}, {ticket_name}, {company_name}, {user_name}, {priority}, {status}, {sla_remaining_minutes}, {answer_last_without_html}, {link_staff}
- $strip_tags() — убирает HTML из значения тега
- Вебхук повторяется 3 раза с интервалом ~30 мин при ошибке (коды 0, 408, 500+)
- Правила Диспетчера могут срабатывать с задержкой 1-20 минут (для условий «Минут от...»)

---

## Настройка HDE — чеклист

1. [ ] Создать отдельного Telegram-бота через @BotFather
2. [ ] Создать группу, включить Темы (Topics)
3. [ ] Добавить бота в группу как администратора с правом manage_topics
4. [ ] Получить chat_id группы через @myidbot → /getgroupid
5. [ ] Получить свой personal chat_id через @myidbot → /getid
6. [ ] Написать боту /start в личку (обязательно! иначе бот не сможет писать в ЛС)
7. [ ] В HDE → Диспетчер → создать 3 правила (см. раздел «Формат JSON от HDE»)
8. [ ] URL всех трёх вебхуков: https://your-domain.com/webhook/hde
9. [ ] Метод: POST, формат: JSON, авторизация: без (секрет в теле)
10. [ ] Режим вебхука: Асинхронный (рекомендуется)
