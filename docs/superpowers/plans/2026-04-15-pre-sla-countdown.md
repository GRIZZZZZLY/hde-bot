# Pre-SLA Countdown (Delete + Resend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить одноразовое pre-SLA уведомление в личный чат на живой счётчик в топике (или General), который каждую минуту удаляет старое сообщение и присылает новое с `-1 минутой`.

**Architecture:** Добавляем `pre_sla_message_id` в БД. `send_pre_sla_alert` отправляет в топик/General и сохраняет `message_id`. Шедулер каждую минуту вызывает `update_pre_sla_alert` — удаляет старое, присылает новое. При ответе оператора/закрытии тикета pre-SLA сообщение удаляется из Telegram.

**Tech Stack:** Python, aiogram 3, aiosqlite, SQLite

---

## Карта файлов

| Файл | Что меняется |
|------|-------------|
| `bot/db.py` | + колонка `pre_sla_message_id`, обновить функции clear/mark/list |
| `bot/formatter.py` | Разделить на `format_pre_sla_alert_topic` и `format_pre_sla_alert_general` |
| `bot/topic_manager.py` | Переписать `send_pre_sla_alert`, добавить `update_pre_sla_alert` и `_try_delete_pre_sla_message` |
| `bot/scheduler.py` | Добавить цикл обновления счётчика |

---

## Task 1: DB — добавить `pre_sla_message_id`

**Files:**
- Modify: `bot/db.py`

- [ ] **Step 1: Добавить колонку в схему**

В `TICKET_TOPIC_COLUMNS` (после `pre_sla_sent_at`, строка ~75):
```python
"pre_sla_message_id": "INTEGER",
```

- [ ] **Step 2: Добавить в UPDATABLE_FIELDS**

После `"pre_sla_sent_at"` в множестве `UPDATABLE_FIELDS` (строка ~96):
```python
"pre_sla_message_id",
```

- [ ] **Step 3: Добавить поле в dataclass TicketTopic**

После `pre_sla_sent_at: Optional[str]` (строка ~119):
```python
pre_sla_message_id: Optional[int]
```

- [ ] **Step 4: Обновить clear_pre_sla — тоже чистить message_id**

```python
async def clear_pre_sla(ticket_id: str) -> None:
    await update_topic(
        ticket_id,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
    )
```

- [ ] **Step 5: Обновить mark_pre_sla_sent — принимать message_id**

```python
async def mark_pre_sla_sent(ticket_id: str, message_id: int, sent_at: Optional[str] = None) -> None:
    await update_topic(
        ticket_id,
        pre_sla_sent_at=sent_at or to_storage(utcnow()),
        pre_sla_message_id=message_id,
    )
```

- [ ] **Step 6: Добавить list_active_pre_sla — тикеты с уже отправленным алертом**

После `list_due_pre_sla` (~строка 770):
```python
async def list_active_pre_sla() -> list[TicketTopic]:
    """Тикеты, у которых pre-SLA уже отправлен и ждёт обновления счётчика."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM ticket_topics
            WHERE topic_state = 'active'
              AND pre_sla_notify_at IS NOT NULL
              AND pre_sla_sent_at IS NOT NULL
              AND pre_sla_message_id IS NOT NULL
            ORDER BY pre_sla_notify_at ASC
            """,
        ) as cursor:
            rows = await cursor.fetchall()
    return [TicketTopic(**dict(row)) for row in rows]
```

- [ ] **Step 7: Запустить миграцию — убедиться, что колонка добавляется**

```bash
python -c "import asyncio; from bot.db import init_db; asyncio.run(init_db())"
```
Ожидаем: выход без ошибок. `init_db` использует `ADD COLUMN IF NOT EXISTS` через `ensure_columns`, новая колонка появится автоматически.

- [ ] **Step 8: Commit**

```bash
git add bot/db.py
git commit -m "feat: add pre_sla_message_id to db schema and update pre-SLA functions"
```

---

## Task 2: Formatter — два шаблона

**Files:**
- Modify: `bot/formatter.py`

- [ ] **Step 1: Заменить format_pre_sla_alert на два отдельных**

Удалить существующую `format_pre_sla_alert` и добавить:

```python
def format_pre_sla_alert_topic(
    minutes_left: int,
    ticket_name: str,
    link: str,
) -> str:
    return (
        f"🔥 SLA через {minutes_left} мин 🔥\n"
        "──────────────\n"
        f"📝 {_escape(ticket_name)}\n"
        f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>'
    )


def format_pre_sla_alert_general(
    minutes_left: int,
    ticket_name: str,
    company_name: str,
    link: str,
) -> str:
    return (
        f"🆘 SLA через {minutes_left} минут — тикет не назначен!\n"
        "──────────────\n"
        f"📝 {_escape(ticket_name)}\n"
        f"🏢 {_escape(company_name)}\n"
        f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>'
    )
```

- [ ] **Step 2: Commit**

```bash
git add bot/formatter.py
git commit -m "feat: split pre-SLA formatter into topic and general variants (A2, B3)"
```

---

## Task 3: topic_manager — переписать логику отправки

**Files:**
- Modify: `bot/topic_manager.py`

- [ ] **Step 1: Обновить импорты в topic_manager.py**

Найти строку с импортом `format_pre_sla_alert` и заменить на:
```python
from .formatter import (
    ...  # остальные импорты без изменений
    format_pre_sla_alert_topic,
    format_pre_sla_alert_general,
)
```

- [ ] **Step 2: Добавить хелпер вычисления оставшихся минут**

Перед `send_pre_sla_alert` добавить:
```python
def _pre_sla_minutes_left(record: db.TicketTopic) -> int:
    """Вычисляет реальные минуты до SLA на основе pre_sla_notify_at + warning_minutes."""
    from .time_utils import parse_datetime, utcnow
    from datetime import timedelta
    deadline = parse_datetime(record.pre_sla_notify_at)
    if deadline is None:
        return config.pre_sla_warning_minutes
    sla_deadline = deadline + timedelta(minutes=config.pre_sla_warning_minutes)
    remaining = (sla_deadline - utcnow()).total_seconds() / 60
    return max(1, round(remaining))
```

- [ ] **Step 3: Добавить хелпер определения места назначения**

```python
def _pre_sla_destination(record: db.TicketTopic) -> tuple[int, int | None]:
    """Возвращает (chat_id, thread_id) для pre-SLA сообщения.
    
    Если тикет назначен — топик тикета.
    Если нет исполнителя — General (general_topic_id).
    """
    has_owner = bool(record.owner_id.strip())
    if has_owner:
        return config.group_chat_id, record.topic_id
    if config.general_topic_id is not None:
        return config.group_chat_id, config.general_topic_id
    # Fallback: если General не настроен, шлём в топик тикета
    return config.group_chat_id, record.topic_id
```

- [ ] **Step 4: Добавить хелпер формирования текста**

```python
def _pre_sla_text(record: db.TicketTopic, minutes_left: int) -> str:
    has_owner = bool(record.owner_id.strip())
    if has_owner:
        return format_pre_sla_alert_topic(
            minutes_left=minutes_left,
            ticket_name=record.ticket_name,
            link=record.hde_link,
        )
    return format_pre_sla_alert_general(
        minutes_left=minutes_left,
        ticket_name=record.ticket_name,
        company_name=record.company_name,
        link=record.hde_link,
    )
```

- [ ] **Step 5: Переписать send_pre_sla_alert**

```python
async def send_pre_sla_alert(bot: Bot, record: db.TicketTopic) -> None:
    minutes_left = _pre_sla_minutes_left(record)
    chat_id, thread_id = _pre_sla_destination(record)
    text = _pre_sla_text(record, minutes_left)

    msg = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await db.mark_pre_sla_sent(record.ticket_id, message_id=msg.message_id)
```

- [ ] **Step 6: Добавить update_pre_sla_alert (delete + resend)**

```python
async def update_pre_sla_alert(bot: Bot, record: db.TicketTopic) -> None:
    """Удаляет старое pre-SLA сообщение и присылает новое с актуальным счётчиком."""
    chat_id, thread_id = _pre_sla_destination(record)

    # Удаляем старое
    if record.pre_sla_message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=record.pre_sla_message_id)
        except TelegramAPIError:
            pass  # уже удалено или недоступно — продолжаем

    minutes_left = _pre_sla_minutes_left(record)
    text = _pre_sla_text(record, minutes_left)

    msg = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await db.mark_pre_sla_sent(record.ticket_id, message_id=msg.message_id)
```

- [ ] **Step 7: Добавить _try_delete_pre_sla_message — удаление при ответе оператора**

```python
async def _try_delete_pre_sla_message(bot: Bot, record: db.TicketTopic) -> None:
    """Удаляет pre-SLA сообщение из Telegram если оно было отправлено."""
    if not record.pre_sla_message_id:
        return
    chat_id, _ = _pre_sla_destination(record)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=record.pre_sla_message_id)
    except TelegramAPIError:
        pass
```

- [ ] **Step 8: Вызывать _try_delete_pre_sla_message перед clear_pre_sla при ответе оператора**

Найти в topic_manager.py места где `pre_sla_notify_at=None, pre_sla_sent_at=None` устанавливается через `update_topic` напрямую (строки ~939-941, ~664-665, ~973-974).

В каждом месте, где доступен `bot` и `record` — добавить перед очисткой:
```python
await _try_delete_pre_sla_message(bot, record)
```

Также добавить `pre_sla_message_id=None` во все inline вызовы `update_topic` где обнуляются pre_sla поля.

- [ ] **Step 9: Обновить экспорт в scheduler.py**

В `bot/scheduler.py` найти строку:
```python
from .topic_manager import delete_pending_topic, send_pre_sla_alert
```
Заменить на:
```python
from .topic_manager import delete_pending_topic, send_pre_sla_alert, update_pre_sla_alert
```

- [ ] **Step 10: Commit**

```bash
git add bot/topic_manager.py
git commit -m "feat: send pre-SLA to topic/general with live countdown (delete+resend)"
```

---

## Task 4: Scheduler — цикл обновления счётчика

**Files:**
- Modify: `bot/scheduler.py`

- [ ] **Step 1: Добавить импорт parse_datetime и list_active_pre_sla**

```python
from .time_utils import to_storage, utcnow, parse_datetime
```

И в импорт из db:
```python
from . import db  # уже есть
```

- [ ] **Step 2: Добавить цикл обновления в process_scheduled_actions**

После блока `list_due_pre_sla` (строка ~193), добавить:

```python
    # Countdown update: каждую ~минуту удаляем старое и присылаем новое
    for record in await db.list_active_pre_sla():
        if record.pre_sla_sent_at:
            last_update = parse_datetime(record.pre_sla_sent_at)
            if last_update and (utcnow() - last_update).total_seconds() < 55:
                continue
        try:
            await update_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error(
                "Failed to update pre-SLA countdown for ticket %s: %s",
                record.ticket_id, exc,
            )
```

- [ ] **Step 3: Проверить вручную**

Запустить бота, дождаться pre-SLA события. Убедиться:
- Первое сообщение появляется в топике или General (в зависимости от owner_id)
- Каждую минуту старое удаляется, приходит новое с `-1 мин`
- При ответе оператора pre-SLA сообщение пропадает

- [ ] **Step 4: Commit**

```bash
git add bot/scheduler.py
git commit -m "feat: add pre-SLA countdown update loop to scheduler (delete+resend every minute)"
```
