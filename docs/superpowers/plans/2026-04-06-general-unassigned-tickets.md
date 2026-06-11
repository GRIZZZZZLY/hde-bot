# General Channel — Unassigned Tickets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Post notifications about unassigned "Оборудование" tickets to the General Telegram topic, edit them on rename, delete them on assignment or closure.

**Architecture:** New isolated module `bot/general_channel.py` handles all General topic logic. New SQLite table persists posted message IDs. Hooks in `hde_webhook.py` call general_channel handlers after existing ones for shared events.

**Tech Stack:** Python 3.11+, aiogram 3, aiosqlite, pytest-asyncio

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `bot/general_channel.py` | Create | All General topic logic: filter, format, send/edit/delete |
| `bot/db.py` | Modify | New table + CRUD for `unassigned_general_messages` |
| `bot/config.py` | Modify | Add `general_topic_id`, `unassigned_department` fields |
| `bot/hde_webhook.py` | Modify | Add `department` to `_normalize_payload`; call general_channel hooks |
| `.env.example` | Modify | Document new env vars |
| `tests/test_general_channel.py` | Create | Unit tests for filter logic and message formatting |

---

### Task 1: Add config fields

**Files:**
- Modify: `bot/config.py`

- [ ] **Step 1: Add fields to Config dataclass**

In `bot/config.py`, add two new fields to the `Config` dataclass (after `digest_night_start_hour_utc`):

```python
general_topic_id: int | None
unassigned_department: str
```

- [ ] **Step 2: Read them in `from_env`**

In the `from_env` classmethod, add inside the `return cls(...)` block:

```python
general_topic_id=(
    int(os.getenv("GENERAL_TOPIC_ID"))
    if os.getenv("GENERAL_TOPIC_ID", "").strip()
    else None
),
unassigned_department=os.getenv("UNASSIGNED_DEPARTMENT", "").strip(),
```

- [ ] **Step 3: Update conftest.py to set the new config fields**

In `tests/conftest.py`, inside `configure_test_settings`, add:

```python
monkeypatch.setattr(cfg, "general_topic_id", None)
monkeypatch.setattr(cfg, "unassigned_department", "")
```

- [ ] **Step 4: Run existing tests to confirm nothing broke**

```
pytest tests/ -v
```

Expected: all tests pass (no new tests yet).

- [ ] **Step 5: Commit**

```bash
git add bot/config.py tests/conftest.py
git commit -m "feat: add general_topic_id and unassigned_department config fields"
```

---

### Task 2: Add `department` field to webhook payload

**Files:**
- Modify: `bot/hde_webhook.py`

- [ ] **Step 1: Add `department` to `_normalize_payload`**

In `bot/hde_webhook.py`, in the `_normalize_payload` function, add this line after the `"link"` entry:

```python
"department": str(payload.get("department") or "").strip(),
```

- [ ] **Step 2: Add a test for the new field**

In `tests/test_hde_payload.py`, add:

```python
def test_normalize_payload_extracts_department():
    payload = _normalize_payload({
        "event_type": "assigned_on_create",
        "ticket_id": "TKT-5",
        "department": "Оборудование",
    })
    assert payload["department"] == "Оборудование"


def test_normalize_payload_department_defaults_to_empty():
    payload = _normalize_payload({
        "event_type": "assigned_on_create",
        "ticket_id": "TKT-6",
    })
    assert payload["department"] == ""
```

- [ ] **Step 3: Run tests**

```
pytest tests/test_hde_payload.py -v
```

Expected: all pass including the two new ones.

- [ ] **Step 4: Commit**

```bash
git add bot/hde_webhook.py tests/test_hde_payload.py
git commit -m "feat: extract department field from HDE webhook payload"
```

---

### Task 3: DB table and CRUD for unassigned_general_messages

**Files:**
- Modify: `bot/db.py`

- [ ] **Step 1: Write failing tests first**

Create `tests/test_general_channel.py`:

```python
import pytest
from bot import db as db_module


@pytest.mark.asyncio
async def test_save_and_get_general_message(initialized_db):
    await db_module.save_general_message("TKT-1", message_id=999, ticket_name="Broken fan")
    row = await db_module.get_general_message("TKT-1")
    assert row is not None
    assert row["message_id"] == 999
    assert row["ticket_name"] == "Broken fan"


@pytest.mark.asyncio
async def test_get_general_message_missing(initialized_db):
    row = await db_module.get_general_message("TKT-MISSING")
    assert row is None


@pytest.mark.asyncio
async def test_delete_general_message(initialized_db):
    await db_module.save_general_message("TKT-2", message_id=100, ticket_name="Old")
    await db_module.delete_general_message("TKT-2")
    row = await db_module.get_general_message("TKT-2")
    assert row is None


@pytest.mark.asyncio
async def test_update_general_message_ticket_name(initialized_db):
    await db_module.save_general_message("TKT-3", message_id=200, ticket_name="Old name")
    await db_module.save_general_message("TKT-3", message_id=200, ticket_name="New name")
    row = await db_module.get_general_message("TKT-3")
    assert row["ticket_name"] == "New name"
```

- [ ] **Step 2: Run tests to confirm they fail**

```
pytest tests/test_general_channel.py -v
```

Expected: AttributeError — `db_module` has no attribute `save_general_message`.

- [ ] **Step 3: Add the table to `init_db` in `bot/db.py`**

Inside `init_db`, after the last `db.execute(CREATE INDEX ...)` block and before `await db.commit()`, add:

```python
await db.execute(
    """
    CREATE TABLE IF NOT EXISTS unassigned_general_messages (
        ticket_id   TEXT PRIMARY KEY,
        message_id  INTEGER NOT NULL,
        ticket_name TEXT DEFAULT '',
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """
)
```

- [ ] **Step 4: Add CRUD functions at the end of `bot/db.py`**

```python
async def save_general_message(ticket_id: str, message_id: int, ticket_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO unassigned_general_messages (ticket_id, message_id, ticket_name)
            VALUES (?, ?, ?)
            ON CONFLICT(ticket_id) DO UPDATE SET
                message_id = excluded.message_id,
                ticket_name = excluded.ticket_name
            """,
            (ticket_id, message_id, ticket_name),
        )
        await db.commit()


async def get_general_message(ticket_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ticket_id, message_id, ticket_name, created_at FROM unassigned_general_messages WHERE ticket_id = ?",
            (ticket_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def delete_general_message(ticket_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM unassigned_general_messages WHERE ticket_id = ?",
            (ticket_id,),
        )
        await db.commit()
```

- [ ] **Step 5: Run tests**

```
pytest tests/test_general_channel.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 6: Run full suite**

```
pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add bot/db.py tests/test_general_channel.py
git commit -m "feat: add unassigned_general_messages table and CRUD"
```

---

### Task 4: Create `bot/general_channel.py`

**Files:**
- Create: `bot/general_channel.py`

- [ ] **Step 1: Write failing tests for filter logic**

Add to `tests/test_general_channel.py`:

```python
from bot.general_channel import _is_unassigned, _format_general_message


def test_is_unassigned_empty_owner():
    assert _is_unassigned(owner_name="", department="Оборудование", target_dept="Оборудование") is True


def test_is_unassigned_explicit_label():
    assert _is_unassigned(owner_name="Неприсвоенный", department="Оборудование", target_dept="Оборудование") is True


def test_is_unassigned_wrong_department():
    assert _is_unassigned(owner_name="", department="Другой", target_dept="Оборудование") is False


def test_is_unassigned_no_department_filter():
    # When target_dept is empty, any department passes
    assert _is_unassigned(owner_name="", department="Anything", target_dept="") is True


def test_is_unassigned_has_owner():
    assert _is_unassigned(owner_name="Иван Петров", department="Оборудование", target_dept="Оборудование") is False


def test_format_general_message_contains_ticket():
    text = _format_general_message(
        display_id="А-12345",
        ticket_name="Сломался принтер",
        link="https://hde.example.com/tickets/1",
    )
    assert "А-12345" in text
    assert "Сломался принтер" in text
    assert "https://hde.example.com/tickets/1" in text
    assert "Неприсвоенный тикет" in text
```

- [ ] **Step 2: Run tests to confirm they fail**

```
pytest tests/test_general_channel.py::test_is_unassigned_empty_owner -v
```

Expected: ModuleNotFoundError — `bot.general_channel` does not exist.

- [ ] **Step 3: Create `bot/general_channel.py`**

```python
"""
Notifications for unassigned tickets in the General Telegram topic.

Enabled only when GENERAL_TOPIC_ID is set in config.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config

logger = logging.getLogger(__name__)


def _is_unassigned(owner_name: str, department: str, target_dept: str) -> bool:
    """Return True if ticket qualifies for General notification."""
    has_owner = bool(owner_name.strip()) and "неприсвоенный" not in owner_name.strip().lower()
    if has_owner:
        return False
    if target_dept and department.strip().lower() != target_dept.strip().lower():
        return False
    return True


def _format_general_message(display_id: str, ticket_name: str, link: str) -> str:
    parts = [
        "🆕 <b>Неприсвоенный тикет</b>\n",
        f"#{display_id} — {ticket_name}",
    ]
    if link:
        parts.append(f'\n<a href="{link}">Открыть в HDE</a>')
    return "\n".join(parts)


async def _send(bot: Bot, text: str) -> int | None:
    """Send a message to the General topic. Returns message_id or None on failure."""
    assert config.general_topic_id is not None
    try:
        msg = await bot.send_message(
            chat_id=config.group_chat_id,
            message_thread_id=config.general_topic_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return msg.message_id
    except TelegramAPIError as exc:
        logger.error("Failed to send General notification: %s", exc)
        return None


async def _edit(bot: Bot, message_id: int, text: str) -> None:
    assert config.general_topic_id is not None
    try:
        await bot.edit_message_text(
            chat_id=config.group_chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to edit General notification %d: %s", message_id, exc)


async def _delete(bot: Bot, message_id: int) -> None:
    try:
        await bot.delete_message(
            chat_id=config.group_chat_id,
            message_id=message_id,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to delete General notification %d: %s", message_id, exc)


def _payload_str(payload: dict, key: str) -> str:
    return str(payload.get(key) or "").strip()


def _display_id(payload: dict) -> str:
    return _payload_str(payload, "unique_id") or _payload_str(payload, "ticket_id")


async def on_assigned_on_create(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    if not _is_unassigned(
        owner_name=_payload_str(payload, "owner_name"),
        department=_payload_str(payload, "department"),
        target_dept=config.unassigned_department,
    ):
        return
    ticket_id = _payload_str(payload, "ticket_id")
    existing = await db.get_general_message(ticket_id)
    if existing:
        return
    text = _format_general_message(
        display_id=_display_id(payload),
        ticket_name=_payload_str(payload, "ticket_name"),
        link=_payload_str(payload, "link"),
    )
    message_id = await _send(bot, text)
    if message_id:
        await db.save_general_message(ticket_id, message_id, _payload_str(payload, "ticket_name"))
        logger.info("Posted General notification for ticket %s (msg_id=%d)", ticket_id, message_id)


async def on_owner_changed(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    ticket_id = _payload_str(payload, "ticket_id")
    owner_name = _payload_str(payload, "owner_name")

    is_now_unassigned = _is_unassigned(
        owner_name=owner_name,
        department=_payload_str(payload, "department"),
        target_dept=config.unassigned_department,
    )

    existing = await db.get_general_message(ticket_id)

    if is_now_unassigned:
        if existing:
            return  # already posted
        text = _format_general_message(
            display_id=_display_id(payload),
            ticket_name=_payload_str(payload, "ticket_name"),
            link=_payload_str(payload, "link"),
        )
        message_id = await _send(bot, text)
        if message_id:
            await db.save_general_message(ticket_id, message_id, _payload_str(payload, "ticket_name"))
            logger.info("Posted General notification on re-unassign for ticket %s", ticket_id)
    else:
        if existing is None:
            return
        await _delete(bot, existing["message_id"])
        await db.delete_general_message(ticket_id)
        logger.info("Deleted General notification for ticket %s (assigned to %s)", ticket_id, owner_name)


async def on_ticket_updated(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    ticket_id = _payload_str(payload, "ticket_id")
    existing = await db.get_general_message(ticket_id)
    if existing is None:
        return
    new_name = _payload_str(payload, "ticket_name")
    if existing["ticket_name"] == new_name:
        return
    text = _format_general_message(
        display_id=_display_id(payload),
        ticket_name=new_name,
        link=_payload_str(payload, "link"),
    )
    await _edit(bot, existing["message_id"], text)
    await db.save_general_message(ticket_id, existing["message_id"], new_name)
    logger.info("Edited General notification for ticket %s", ticket_id)


async def on_ticket_closed(bot: Bot, payload: dict) -> None:
    if config.general_topic_id is None:
        return
    ticket_id = _payload_str(payload, "ticket_id")
    existing = await db.get_general_message(ticket_id)
    if existing is None:
        return
    await _delete(bot, existing["message_id"])
    await db.delete_general_message(ticket_id)
    logger.info("Deleted General notification on close for ticket %s", ticket_id)
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_general_channel.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Run full suite**

```
pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add bot/general_channel.py tests/test_general_channel.py
git commit -m "feat: add general_channel module with unassigned ticket notifications"
```

---

### Task 5: Wire general_channel into hde_webhook.py

**Files:**
- Modify: `bot/hde_webhook.py`

- [ ] **Step 1: Import general_channel handlers at top of `bot/hde_webhook.py`**

After the existing imports, add:

```python
from . import general_channel
```

- [ ] **Step 2: Call general_channel hooks after existing handlers in `hde_webhook_handler`**

In `hde_webhook_handler`, after `await handler(bot, payload)` succeeds, add the general_channel dispatch. Replace the try/except block that calls `handler`:

```python
try:
    await handler(bot, payload)
except Exception as exc:
    logger.exception("Error handling event '%s': %s", event_type, exc)
    return web.Response(status=500, text="Internal error")

# General channel hooks — run after main handler, failures are non-fatal
try:
    if event_type == "assigned_on_create":
        await general_channel.on_assigned_on_create(bot, payload)
    elif event_type == "owner_changed":
        await general_channel.on_owner_changed(bot, payload)
    elif event_type == "ticket_updated":
        await general_channel.on_ticket_updated(bot, payload)
    elif event_type == "ticket_closed":
        await general_channel.on_ticket_closed(bot, payload)
except Exception as exc:
    logger.exception("General channel hook failed for event '%s': %s", event_type, exc)
```

- [ ] **Step 3: Run full test suite**

```
pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add bot/hde_webhook.py
git commit -m "feat: wire general_channel hooks into hde_webhook handler"
```

---

### Task 6: Document env vars in .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add the new variables**

Open `.env.example` and add a new section at the end:

```env
# General topic notifications (unassigned tickets)
# Set GENERAL_TOPIC_ID to the message_thread_id of the General topic to enable this feature
GENERAL_TOPIC_ID=
# Optional: only notify for tickets in this department (leave empty for all departments)
UNASSIGNED_DEPARTMENT=Оборудование
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: document GENERAL_TOPIC_ID and UNASSIGNED_DEPARTMENT env vars"
```

---

## Self-Review

**Spec coverage:**
- ✅ Post notification on new unassigned ticket → `on_assigned_on_create`
- ✅ Edit on ticket rename → `on_ticket_updated`
- ✅ Delete on assignment → `on_owner_changed` (has real owner branch)
- ✅ Delete on close → `on_ticket_closed`
- ✅ Re-unassign creates new notification → `on_owner_changed` (unassigned branch)
- ✅ Filter by owner_name empty/"Неприсвоенный" → `_is_unassigned`
- ✅ Filter by department → `_is_unassigned`
- ✅ Disabled when GENERAL_TOPIC_ID not set → guard in every public function
- ✅ Message format with #ID, name, link → `_format_general_message`
- ✅ DB persistence of message_id → Task 3

**Placeholder scan:** None found.

**Type consistency:** `get_general_message` returns `dict | None`, used consistently as `existing["message_id"]` and `existing["ticket_name"]` throughout.
