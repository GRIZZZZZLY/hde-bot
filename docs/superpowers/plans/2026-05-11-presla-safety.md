# Pre-SLA Safety Net — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить ложные pre-SLA алерты при опоздавших вебхуках и добавить автоответ клиенту за N минут до сгорания SLA.

**Architecture:**
1. Перед каждым pre-SLA алертом запрашивать HDE API — если оператор уже ответил, самовосстановить состояние и пропустить алерт.
2. За `REASSURANCE_MINUTES_BEFORE` минут до дедлайна автоматически отправлять клиенту сообщение через HDE API.

**Spec:** `docs/superpowers/specs/2026-05-11-presla-safety-design.md`

**Tech Stack:** Python 3.10+, aiosqlite, aiohttp, aiogram 3.x

---

## File Map

| Файл | Изменения |
|---|---|
| `bot/db.py` | + колонка `reassurance_sent_at`, очистка в `clear_pre_sla` |
| `bot/config.py` | + `reassurance_minutes_before`, `reassurance_text`, `presla_hde_verify` |
| `bot/topic_manager.py` | + `_hde_staff_replied_since()`, + `send_reassurance_to_client()` |
| `bot/scheduler.py` | Guard в обоих pre-SLA циклах; новый цикл автоответа |
| `.env.example` | Документировать новые переменные |
| `tests/test_presla_safety.py` | Новые тесты |

---

## Task 1: DB — колонка `reassurance_sent_at`

**Files:**
- Modify: `bot/db.py`

- [ ] **Step 1: Написать тест**

```python
# tests/test_presla_safety.py
import asyncio
import pytest
import aiosqlite
from bot import db as db_module
from bot.time_utils import utcnow, to_storage

@pytest.fixture
async def initialized_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    await db_module.init_db()
    yield db_path

@pytest.mark.asyncio
async def test_reassurance_sent_at_column_exists(initialized_db):
    """reassurance_sent_at column exists and defaults to NULL."""
    await db_module.upsert_topic(
        "TKT-RSA",
        100,
        unique_id="X-1",
        company_name="ACME",
        ticket_name="Test",
        priority="medium",
        status="open",
        owner_id="1",
        owner_name="Op",
        hde_link="https://example.com",
    )
    record = await db_module.get_topic("TKT-RSA")
    assert record is not None
    assert record.reassurance_sent_at is None

@pytest.mark.asyncio
async def test_clear_pre_sla_also_clears_reassurance(initialized_db):
    """clear_pre_sla resets reassurance_sent_at too."""
    now_str = to_storage(utcnow())
    await db_module.upsert_topic(
        "TKT-RSB",
        101,
        unique_id="X-2",
        company_name="ACME",
        ticket_name="Test",
        priority="medium",
        status="open",
        owner_id="1",
        owner_name="Op",
        hde_link="https://example.com",
    )
    await db_module.update_topic("TKT-RSB", reassurance_sent_at=now_str)
    await db_module.clear_pre_sla("TKT-RSB")
    record = await db_module.get_topic("TKT-RSB")
    assert record.reassurance_sent_at is None
```

- [ ] **Step 2: Убедиться что тесты падают**

```bash
pytest tests/test_presla_safety.py -v
```

Ожидается: `AttributeError` или `OperationalError` — колонки нет.

- [ ] **Step 3: Добавить `reassurance_sent_at` в `TICKET_TOPIC_COLUMNS`**

В `bot/db.py` найти `TICKET_TOPIC_COLUMNS` (словарь со столбцами). Добавить после `pre_sla_message_id`:

```python
"reassurance_sent_at": "TEXT",
```

- [ ] **Step 4: Добавить `reassurance_sent_at` в `UPDATABLE_FIELDS`**

В множестве `UPDATABLE_FIELDS` добавить `"reassurance_sent_at"`.

- [ ] **Step 5: Добавить поле в dataclass `TicketTopic`**

После `pre_sla_message_id: Optional[int]`:

```python
reassurance_sent_at: Optional[str] = None
```

- [ ] **Step 6: Обновить `clear_pre_sla` — чистить `reassurance_sent_at`**

Найти функцию `clear_pre_sla` в `bot/db.py`. Добавить `reassurance_sent_at=None` в вызов `update_topic`:

```python
async def clear_pre_sla(ticket_id: str) -> None:
    await update_topic(
        ticket_id,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
        reassurance_sent_at=None,
    )
```

- [ ] **Step 7: Запустить тесты**

```bash
pytest tests/test_presla_safety.py -v
```

Ожидается: `2 passed`.

- [ ] **Step 8: Прогнать полный suite**

```bash
pytest tests/ -q
```

- [ ] **Step 9: Commit**

```bash
git add bot/db.py tests/test_presla_safety.py
git commit -m "feat(db): add reassurance_sent_at column, clear in clear_pre_sla"
```

---

## Task 2: Config — новые переменные

**Files:**
- Modify: `bot/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Добавить поля в `Config` dataclass**

После `groq_api_key: str` добавить:

```python
presla_hde_verify: bool
reassurance_minutes_before: int
reassurance_text: str
```

- [ ] **Step 2: Читать в `from_env`**

```python
presla_hde_verify=os.getenv("PRESLA_HDE_VERIFY", "1") == "1",
reassurance_minutes_before=int(os.getenv("REASSURANCE_MINUTES_BEFORE", "2")),
reassurance_text=os.getenv(
    "REASSURANCE_TEXT",
    "Я про вас не забыл, занимаюсь вашим вопросом 🔧",
),
```

- [ ] **Step 3: Добавить в `.env.example`**

```dotenv
# Pre-SLA safety net
PRESLA_HDE_VERIFY=1           # 1 = verify with HDE before firing alert; 0 = skip check
REASSURANCE_MINUTES_BEFORE=2  # auto-reply to client this many minutes before SLA deadline
REASSURANCE_TEXT=Я про вас не забыл, занимаюсь вашим вопросом 🔧
```

- [ ] **Step 4: Проверить синтаксис**

```bash
python -c "from bot.config import Config; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add bot/config.py .env.example
git commit -m "feat(config): add presla_hde_verify, reassurance_minutes_before, reassurance_text"
```

---

## Task 3: topic_manager — `_hde_staff_replied_since` и `send_reassurance_to_client`

**Files:**
- Modify: `bot/topic_manager.py`
- Modify: `tests/test_presla_safety.py`

### Логика staff-check

`HDEPost.user_id` — числовой. `config.hde_owner_id` — строка (`"12345"` или `""`).
Staff = `post.user_id == int(config.hde_owner_id)` при условии что `hde_owner_id` непустой.
Если `hde_owner_id` пустой → skip check (fail-open, вернуть `False`).

- [ ] **Step 1: Добавить тест `_hde_staff_replied_since`**

```python
# tests/test_presla_safety.py (добавить внизу)
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import timedelta
from bot.time_utils import utcnow, to_storage

@pytest.mark.asyncio
async def test_hde_staff_replied_since_true():
    """Returns True when last post is from operator after client reply."""
    from bot.hde_api import HDEPost
    from bot.topic_manager import _hde_staff_replied_since

    client_reply_at = utcnow() - timedelta(minutes=30)
    staff_post_date = (utcnow() - timedelta(minutes=25)).strftime("%d.%m.%Y %H:%M")

    post = HDEPost(post_id=1, user_id=42, text="reply", date=staff_post_date)

    with patch("bot.topic_manager.HDEApiClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(return_value=MockClient.return_value)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
        instance = MagicMock()
        instance.get_ticket_posts = AsyncMock(return_value=[post])
        MockClient.return_value = instance
        with patch("bot.topic_manager.config") as mock_cfg:
            mock_cfg.hde_owner_id = "42"
            mock_cfg.presla_hde_verify = True
            result = await _hde_staff_replied_since("TKT-1", to_storage(client_reply_at))

    assert result is True

@pytest.mark.asyncio
async def test_hde_staff_replied_since_false_when_client_last():
    """Returns False when last post is from client (not operator)."""
    from bot.hde_api import HDEPost
    from bot.topic_manager import _hde_staff_replied_since

    client_reply_at = utcnow() - timedelta(minutes=5)
    client_post_date = (utcnow() - timedelta(minutes=4)).strftime("%d.%m.%Y %H:%M")

    post = HDEPost(post_id=2, user_id=99, text="another msg", date=client_post_date)

    with patch("bot.topic_manager.HDEApiClient") as MockClient:
        instance = MagicMock()
        instance.get_ticket_posts = AsyncMock(return_value=[post])
        MockClient.return_value = instance
        with patch("bot.topic_manager.config") as mock_cfg:
            mock_cfg.hde_owner_id = "42"
            mock_cfg.presla_hde_verify = True
            result = await _hde_staff_replied_since("TKT-1", to_storage(client_reply_at))

    assert result is False

@pytest.mark.asyncio
async def test_hde_staff_replied_since_returns_false_on_error():
    """Returns False (fail-open) if HDE API call fails."""
    from bot.topic_manager import _hde_staff_replied_since
    from bot.time_utils import to_storage, utcnow

    with patch("bot.topic_manager.HDEApiClient") as MockClient:
        instance = MagicMock()
        instance.get_ticket_posts = AsyncMock(side_effect=Exception("HDE down"))
        MockClient.return_value = instance
        with patch("bot.topic_manager.config") as mock_cfg:
            mock_cfg.hde_owner_id = "42"
            mock_cfg.presla_hde_verify = True
            result = await _hde_staff_replied_since("TKT-1", to_storage(utcnow()))

    assert result is False
```

- [ ] **Step 2: Реализовать `_hde_staff_replied_since`**

В `bot/topic_manager.py` добавить перед `send_pre_sla_alert`:

```python
async def _hde_staff_replied_since(ticket_id: str, since_storage: Optional[str]) -> bool:
    """Return True if operator has posted in HDE after the last client reply.

    Calls HDE API. On any error returns False (fail-open: prefer false alert over silence).
    Skips check if presla_hde_verify=False or hde_owner_id not configured.
    """
    if not config.presla_hde_verify:
        return False
    owner_id_str = config.hde_owner_id.strip()
    if not owner_id_str:
        return False
    try:
        owner_id = int(owner_id_str)
    except ValueError:
        return False

    since_dt = parse_datetime(since_storage) if since_storage else None

    try:
        from .hde_api import HDEApiClient
        client = HDEApiClient()
        posts = await client.get_ticket_posts(ticket_id, limit=5)
        for post in reversed(posts):  # newest last after get_ticket_posts sort
            post_dt = parse_datetime(post.date)
            if post.user_id == owner_id:
                if since_dt is None or (post_dt and post_dt > since_dt):
                    return True
        return False
    except Exception as exc:
        logger.warning("pre-SLA HDE verify failed for ticket %s: %s", ticket_id, exc)
        return False
```

> Примечание: `get_ticket_posts` возвращает посты oldest-first (из комментария в hde_api.py).
> `reversed(posts)` = newest first, берём первый от operator = самый свежий.

- [ ] **Step 3: Реализовать `send_reassurance_to_client`**

Добавить после `_hde_staff_replied_since`:

```python
async def send_reassurance_to_client(bot: Bot, record: db.TicketTopic) -> None:
    """Send reassurance message to client via HDE and notify topic."""
    from .hde_api import HDEApiClient, HDEApiError
    try:
        client = HDEApiClient()
        await client.add_post(record.ticket_id, config.reassurance_text)
    except HDEApiError as exc:
        logger.warning("Reassurance post failed for ticket %s: %s", record.ticket_id, exc)
        return  # don't mark sent — retry next tick

    await db.update_topic(record.ticket_id, reassurance_sent_at=to_storage(utcnow()))

    try:
        await bot.send_message(
            chat_id=config.group_chat_id,
            message_thread_id=record.topic_id,
            text=(
                "🤖 <b>Автоответ клиенту отправлен</b>\n"
                f"<i>{config.reassurance_text}</i>"
            ),
            parse_mode="HTML",
            disable_notification=True,
        )
    except TelegramAPIError as exc:
        logger.warning("Failed to notify topic about reassurance for %s: %s", record.ticket_id, exc)
```

- [ ] **Step 4: Добавить импорт `HDEApiClient` если нужен**

```bash
grep -n "HDEApiClient" bot/topic_manager.py | head -5
```

Если нет — добавить в локальный импорт внутри функции (уже есть в `_post_client_history` как локальный — ок).

- [ ] **Step 5: Запустить тесты**

```bash
pytest tests/test_presla_safety.py -v
```

- [ ] **Step 6: Прогнать suite**

```bash
pytest tests/ -q
```

- [ ] **Step 7: Commit**

```bash
git add bot/topic_manager.py tests/test_presla_safety.py
git commit -m "feat(topic_manager): add _hde_staff_replied_since guard and send_reassurance_to_client"
```

---

## Task 4: Scheduler — guard + цикл автоответа

**Files:**
- Modify: `bot/scheduler.py`
- Modify: `tests/test_presla_safety.py`

- [ ] **Step 1: Добавить тест guard**

```python
# tests/test_presla_safety.py (добавить внизу)
@pytest.mark.asyncio
async def test_scheduler_skips_presla_when_staff_already_replied(initialized_db, monkeypatch):
    """Scheduler clears pre-SLA and skips alert when operator replied in HDE."""
    import bot.work_schedule as work_schedule
    monkeypatch.setattr(work_schedule, "is_work_time", lambda: True)
    monkeypatch.setattr(work_schedule, "is_work_day", lambda: True)
    monkeypatch.setattr(work_schedule, "was_yesterday_work_day", lambda: False)

    await db_module.upsert_topic(
        "TKT-GUARD",
        200,
        unique_id="G-1",
        company_name="ACME",
        ticket_name="Test",
        priority="medium",
        status="open",
        owner_id="42",
        owner_name="Op",
        hde_link="https://example.com",
        pre_sla_notify_at=to_storage(utcnow() - timedelta(minutes=1)),
        last_client_reply_at=to_storage(utcnow() - timedelta(minutes=30)),
    )

    from bot.scheduler import process_scheduled_actions
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()

    with patch("bot.scheduler._hde_staff_replied_since", new=AsyncMock(return_value=True)):
        with patch("bot.scheduler.db", db_module):
            await process_scheduled_actions(bot_mock)

    record = await db_module.get_topic("TKT-GUARD")
    assert record.pre_sla_notify_at is None  # cleared
    assert not bot_mock.send_message.called   # no alert sent
```

- [ ] **Step 2: Обновить импорты в `bot/scheduler.py`**

Найти строку:
```python
from .topic_manager import delete_pending_topic, send_pre_sla_alert, update_pre_sla_alert
```

Добавить:
```python
from .topic_manager import (
    delete_pending_topic,
    send_pre_sla_alert,
    update_pre_sla_alert,
    _hde_staff_replied_since,
    send_reassurance_to_client,
)
```

- [ ] **Step 3: Добавить guard в цикл `list_due_pre_sla`**

Найти в `bot/scheduler.py` блок (строки ~314-318):
```python
    for record in await db.list_due_pre_sla(now_value):
        try:
            await send_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error("Failed to send pre-SLA alert for ticket %s: %s", record.ticket_id, exc)
```

Заменить на:
```python
    for record in await db.list_due_pre_sla(now_value):
        if await _hde_staff_replied_since(record.ticket_id, record.last_client_reply_at):
            logger.info(
                "pre-SLA skipped for ticket %s: operator already replied in HDE (self-heal)",
                record.ticket_id,
            )
            await db.clear_pre_sla(record.ticket_id)
            continue
        try:
            await send_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error("Failed to send pre-SLA alert for ticket %s: %s", record.ticket_id, exc)
```

- [ ] **Step 4: Добавить guard в цикл `list_active_pre_sla`**

Найти блок (строки ~320-329):
```python
    for record in await db.list_active_pre_sla():
        if record.pre_sla_sent_at:
            last_update = parse_datetime(record.pre_sla_sent_at)
            if last_update and (utcnow() - last_update).total_seconds() < 55:
                continue
        try:
            await update_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error(...)
```

После `continue` (rate-limit check) добавить guard перед `update_pre_sla_alert`:
```python
    for record in await db.list_active_pre_sla():
        if record.pre_sla_sent_at:
            last_update = parse_datetime(record.pre_sla_sent_at)
            if last_update and (utcnow() - last_update).total_seconds() < 55:
                continue
        if await _hde_staff_replied_since(record.ticket_id, record.last_client_reply_at):
            logger.info(
                "pre-SLA countdown cleared for ticket %s: operator replied in HDE",
                record.ticket_id,
            )
            await _try_delete_pre_sla_message_safe(bot, record)
            await db.clear_pre_sla(record.ticket_id)
            continue
        try:
            await update_pre_sla_alert(bot, record)
        except TelegramAPIError as exc:
            logger.error(
                "Failed to update pre-SLA countdown for ticket %s: %s",
                record.ticket_id,
                exc,
            )
```

Для `_try_delete_pre_sla_message_safe` — добавить в импорт из topic_manager:
```python
    _try_delete_pre_sla_message,
```
и обернуть:
```python
async def _try_delete_pre_sla_message_safe(bot: Bot, record: db.TicketTopic) -> None:
    try:
        await _try_delete_pre_sla_message(bot, record)
    except Exception:
        pass
```

- [ ] **Step 5: Добавить цикл автоответа**

После блока `list_active_pre_sla`, добавить:
```python
    # Auto-reassurance: send client message N minutes before SLA deadline
    from .config import config as _config
    for record in await db.list_active_pre_sla():
        if record.reassurance_sent_at is not None:
            continue
        minutes_left = _pre_sla_minutes_left_for(record)
        if minutes_left > _config.reassurance_minutes_before:
            continue
        if await _hde_staff_replied_since(record.ticket_id, record.last_client_reply_at):
            await db.clear_pre_sla(record.ticket_id)
            continue
        try:
            await send_reassurance_to_client(bot, record)
        except Exception as exc:
            logger.warning("Reassurance failed for ticket %s: %s", record.ticket_id, exc)
```

Добавить хелпер `_pre_sla_minutes_left_for` в scheduler (или импортировать из topic_manager если там уже есть):

```bash
grep -n "_pre_sla_minutes_left" bot/topic_manager.py | head -5
```

Если уже есть `_pre_sla_minutes_left` — добавить в импорт. Если нет — добавить в scheduler:

```python
def _pre_sla_minutes_left_for(record: db.TicketTopic) -> float:
    from .time_utils import parse_datetime, utcnow
    from .config import config
    from datetime import timedelta
    deadline = parse_datetime(record.pre_sla_notify_at)
    if deadline is None:
        return float("inf")
    sla_deadline = deadline + timedelta(minutes=config.pre_sla_warning_minutes)
    remaining = (sla_deadline - utcnow()).total_seconds() / 60
    return max(0.0, remaining)
```

- [ ] **Step 6: Проверить синтаксис**

```bash
python -c "import bot.scheduler; print('OK')"
```

- [ ] **Step 7: Запустить тесты**

```bash
pytest tests/test_presla_safety.py -v
```

- [ ] **Step 8: Прогнать suite**

```bash
pytest tests/ -q
```

- [ ] **Step 9: Commit**

```bash
git add bot/scheduler.py tests/test_presla_safety.py
git commit -m "feat(scheduler): guard pre-SLA alerts with HDE verify + auto-reassurance at T-Nmin"
```

---

## Итог

После этих трёх задач:

- Ложный pre-SLA при опоздавшем webhook → бот проверит HDE, увидит ответ оператора, самовосстановится и промолчит.
- Потерянный `staff_reply` webhook → тот же guard в countdown-цикле подберёт и почистит.
- За 2 минуты (настраивается) до сгорания SLA → клиент автоматически получает «Я про вас не забыл», только если оператор реально молчал.
