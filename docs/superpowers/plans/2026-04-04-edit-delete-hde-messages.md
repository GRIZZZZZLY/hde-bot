# Edit/Delete HDE Messages from Telegram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow operators to edit and delete their own replies/notes in HDE by editing or using `/delete` command on Telegram messages sent via `/send` or `/note`.

**Architecture:** Add a `sent_hde_messages` table that maps each Telegram message ID to its HDE entity ID (post or comment). After every `/send` or `/note`, store this mapping. On `edited_message` events, look up the HDE ID and call `PUT`. On `/delete` command (reply to the message), call `DELETE`. Telegram does not notify bots about message deletions, so `/delete` command is the only way to delete in HDE.

**Tech Stack:** Python 3.12, aiogram 3.x, aiohttp, aiosqlite, pytest-asyncio

---

## File Map

| File | Change |
|------|--------|
| `bot/db.py` | Add `sent_hde_messages` table, `SentHdeMessage` dataclass, CRUD functions |
| `bot/hde_api.py` | Add `update_post`, `delete_post`, `update_comment`, `delete_comment` methods |
| `bot/formatter.py` | Add `format_message_edited`, `format_message_deleted` |
| `bot/operator_replies.py` | Save mapping after send/note; add `edit_operator_message`, `delete_operator_message` |
| `bot/handlers/commands.py` | Add `edited_message` handler and `/delete` command |
| `tests/test_operator_replies.py` | Tests for edit/delete flows |

---

## Task 1: DB — `sent_hde_messages` table and CRUD

**Files:**
- Modify: `bot/db.py`

### What to add

New dataclass at the top of `db.py` (after `CachedTopicMedia`):

```python
@dataclass
class SentHdeMessage:
    telegram_message_id: int
    topic_id: int
    ticket_id: str
    hde_entity_id: int      # numeric ID in HDE (from response data.id)
    entity_type: str        # 'post' (public reply) or 'comment' (internal note)
    created_at: str
```

### Table creation

Add inside the `async with aiosqlite.connect(DB_PATH) as db:` block in `init_db()`, **after** the `topic_media_cache` table creation and before `await db.commit()`:

```python
await db.execute(
    """
    CREATE TABLE IF NOT EXISTS sent_hde_messages (
        telegram_message_id  INTEGER NOT NULL,
        topic_id             INTEGER NOT NULL,
        ticket_id            TEXT NOT NULL,
        hde_entity_id        INTEGER NOT NULL,
        entity_type          TEXT NOT NULL,
        created_at           TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (telegram_message_id, topic_id)
    )
    """
)
```

### CRUD functions

Add at the end of `db.py`:

```python
async def save_sent_message(
    telegram_message_id: int,
    topic_id: int,
    ticket_id: str,
    hde_entity_id: int,
    entity_type: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO sent_hde_messages
                (telegram_message_id, topic_id, ticket_id, hde_entity_id, entity_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_message_id, topic_id, ticket_id, hde_entity_id, entity_type),
        )
        await db.commit()


async def get_sent_message(
    telegram_message_id: int,
    topic_id: int,
) -> Optional[SentHdeMessage]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM sent_hde_messages
            WHERE telegram_message_id = ? AND topic_id = ?
            """,
            (telegram_message_id, topic_id),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return SentHdeMessage(
        telegram_message_id=row["telegram_message_id"],
        topic_id=row["topic_id"],
        ticket_id=row["ticket_id"],
        hde_entity_id=row["hde_entity_id"],
        entity_type=row["entity_type"],
        created_at=row["created_at"],
    )


async def delete_sent_message(telegram_message_id: int, topic_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM sent_hde_messages WHERE telegram_message_id = ? AND topic_id = ?",
            (telegram_message_id, topic_id),
        )
        await db.commit()
```

- [ ] **Step 1: Write the failing test**

In `tests/test_operator_replies.py`, add at the end:

```python
# ── sent_hde_messages DB tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_and_get_sent_message(initialized_db):
    await db_module.save_sent_message(
        telegram_message_id=111,
        topic_id=9001,
        ticket_id="TKT-100",
        hde_entity_id=42,
        entity_type="post",
    )
    record = await db_module.get_sent_message(111, 9001)
    assert record is not None
    assert record.hde_entity_id == 42
    assert record.entity_type == "post"
    assert record.ticket_id == "TKT-100"


@pytest.mark.asyncio
async def test_get_sent_message_returns_none_when_missing(initialized_db):
    result = await db_module.get_sent_message(999, 9001)
    assert result is None


@pytest.mark.asyncio
async def test_delete_sent_message(initialized_db):
    await db_module.save_sent_message(111, 9001, "TKT-100", 42, "post")
    await db_module.delete_sent_message(111, 9001)
    result = await db_module.get_sent_message(111, 9001)
    assert result is None
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_operator_replies.py::test_save_and_get_sent_message tests/test_operator_replies.py::test_get_sent_message_returns_none_when_missing tests/test_operator_replies.py::test_delete_sent_message -v
```

Expected: `AttributeError: module 'bot.db' has no attribute 'save_sent_message'`

- [ ] **Step 3: Implement** — make the changes to `bot/db.py` described above (dataclass + table creation + 3 CRUD functions)

- [ ] **Step 4: Run to verify they pass**

```
pytest tests/test_operator_replies.py::test_save_and_get_sent_message tests/test_operator_replies.py::test_get_sent_message_returns_none_when_missing tests/test_operator_replies.py::test_delete_sent_message -v
```

Expected: `3 passed`

- [ ] **Step 5: Run full suite to check no regressions**

```
pytest tests/ -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add bot/db.py tests/test_operator_replies.py
git commit -m "feat: add sent_hde_messages table and CRUD for message mapping"
```

---

## Task 2: HDE API — PUT and DELETE methods

**Files:**
- Modify: `bot/hde_api.py`

### What to add

Inside `HDEApiClient`, after `add_post`:

```python
async def update_post(self, ticket_id: str, post_id: int, text: str) -> HDEApiResult:
    return await self._put(f"/tickets/{ticket_id}/posts/{post_id}/", text=text)

async def delete_post(self, ticket_id: str, post_id: int) -> HDEApiResult:
    return await self._delete(f"/tickets/{ticket_id}/posts/{post_id}/")

async def update_comment(self, ticket_id: str, comment_id: int, text: str) -> HDEApiResult:
    return await self._put(f"/tickets/{ticket_id}/comments/{comment_id}/", text=text)

async def delete_comment(self, ticket_id: str, comment_id: int) -> HDEApiResult:
    return await self._delete(f"/tickets/{ticket_id}/comments/{comment_id}/")
```

Add private methods `_put` and `_delete` after `_post`:

```python
async def _put(self, path: str, *, text: str = "") -> HDEApiResult:
    url = f"{self.base_url}{path}"
    payload = {"text": text.strip()}
    async with aiohttp.ClientSession(auth=self.auth) as session:
        async with session.put(url, data=payload) as response:
            data = await self._read_response(response)
            if response.status >= 400:
                message = self._extract_error_message(data) or f"HDE API error {response.status}"
                raise HDEApiError(message)
            return HDEApiResult(status=response.status, data=data)

async def _delete(self, path: str) -> HDEApiResult:
    url = f"{self.base_url}{path}"
    async with aiohttp.ClientSession(auth=self.auth) as session:
        async with session.delete(url) as response:
            data = await self._read_response(response)
            if response.status >= 400:
                message = self._extract_error_message(data) or f"HDE API error {response.status}"
                raise HDEApiError(message)
            return HDEApiResult(status=response.status, data=data)
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_operator_replies.py`:

```python
@pytest.mark.asyncio
async def test_hde_api_update_post_calls_put(monkeypatch):
    calls = []

    async def fake_put(self, path, *, text=""):
        calls.append(("PUT", path, text))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient._put", fake_put)
    client = HDEApiClient()
    await client.update_post("TKT-100", 42, "updated text")
    assert calls == [("PUT", "/tickets/TKT-100/posts/42/", "updated text")]


@pytest.mark.asyncio
async def test_hde_api_delete_post_calls_delete(monkeypatch):
    calls = []

    async def fake_delete(self, path):
        calls.append(("DELETE", path))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient._delete", fake_delete)
    client = HDEApiClient()
    await client.delete_post("TKT-100", 42)
    assert calls == [("DELETE", "/tickets/TKT-100/posts/42/")]
```

(Add the import `from bot.hde_api import HDEApiClient` if not already present — check the existing imports at the top of the test file.)

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_operator_replies.py::test_hde_api_update_post_calls_put tests/test_operator_replies.py::test_hde_api_delete_post_calls_delete -v
```

Expected: `AttributeError: 'HDEApiClient' object has no attribute 'update_post'`

- [ ] **Step 3: Implement** — make changes to `bot/hde_api.py` as described above

- [ ] **Step 4: Run to verify they pass**

```
pytest tests/test_operator_replies.py::test_hde_api_update_post_calls_put tests/test_operator_replies.py::test_hde_api_delete_post_calls_delete -v
```

Expected: `2 passed`

- [ ] **Step 5: Run full suite**

```
pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git add bot/hde_api.py tests/test_operator_replies.py
git commit -m "feat: add update/delete methods to HDEApiClient (PUT, DELETE)"
```

---

## Task 3: Formatter — edit/delete confirmation messages

**Files:**
- Modify: `bot/formatter.py`

Add at the end of `formatter.py`:

```python
def format_message_edited(display_id: str) -> str:
    return (
        "✏️ <b>Сообщение обновлено в HDE</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>"
    )


def format_message_deleted(display_id: str) -> str:
    return (
        "🗑️ <b>Сообщение удалено из HDE</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>"
    )
```

- [ ] **Step 1: Write the failing tests**

In `tests/test_hde_payload.py`, add:

```python
from bot.formatter import format_message_edited, format_message_deleted

def test_format_message_edited_contains_id():
    text = format_message_edited("ABC-123")
    assert "обновлено" in text
    assert "ABC-123" in text

def test_format_message_deleted_contains_id():
    text = format_message_deleted("ABC-123")
    assert "удалено" in text
    assert "ABC-123" in text
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_hde_payload.py::test_format_message_edited_contains_id tests/test_hde_payload.py::test_format_message_deleted_contains_id -v
```

Expected: `ImportError: cannot import name 'format_message_edited'`

- [ ] **Step 3: Implement** — add the two functions to `bot/formatter.py`

- [ ] **Step 4: Run to verify they pass**

```
pytest tests/test_hde_payload.py::test_format_message_edited_contains_id tests/test_hde_payload.py::test_format_message_deleted_contains_id -v
```

- [ ] **Step 5: Commit**

```bash
git add bot/formatter.py tests/test_hde_payload.py
git commit -m "feat: add format_message_edited and format_message_deleted"
```

---

## Task 4: operator_replies.py — save mapping, edit, delete

**Files:**
- Modify: `bot/operator_replies.py`

### 4a: Save mapping after send/note

In `send_public_reply`, after the `await client.add_post(...)` call succeeds, extract the HDE post ID and save the mapping. Currently the code is:

```python
# in send_public_reply (bot/operator_replies.py)
    client = HDEApiClient()
    try:
        await client.add_post(
            context.record.ticket_id,
            text=payload.text,
            attachments=payload.attachments,
        )
    except HDEApiError as exc:
        raise OperatorReplyError(str(exc)) from exc

    return format_reply_sent(context.record.unique_id)
```

Change to:

```python
    client = HDEApiClient()
    try:
        result = await client.add_post(
            context.record.ticket_id,
            text=payload.text,
            attachments=payload.attachments,
        )
    except HDEApiError as exc:
        raise OperatorReplyError(str(exc)) from exc

    hde_post_id = _extract_hde_id(result)
    if hde_post_id is not None and tg_message_id is not None:
        await db.save_sent_message(
            telegram_message_id=tg_message_id,
            topic_id=context.topic_id,
            ticket_id=context.record.ticket_id,
            hde_entity_id=hde_post_id,
            entity_type="post",
        )

    return format_reply_sent(context.record.unique_id)
```

Do the same for `add_internal_note` (which calls `client.add_comment`), saving `entity_type="comment"`.

Add this helper at the module level (before the class definitions):

```python
def _extract_hde_id(result: Any) -> Optional[int]:
    """Extract numeric ID from HDE API response: {"data": {"id": 42, ...}}"""
    if isinstance(result, object) and hasattr(result, "data"):
        data = result.data
        if isinstance(data, dict):
            inner = data.get("data", {})
            if isinstance(inner, dict):
                return inner.get("id")
    return None
```

Add `from typing import Any` to imports if not present. Add `from .formatter import ..., format_message_edited, format_message_deleted` to the formatter imports.

**Thread-safety note:** `send_public_reply` and `add_internal_note` need `tg_message_id: Optional[int]` added as parameter. Update their signatures:

```python
async def send_public_reply(
    *,
    bot: Bot,
    context: OperatorTopicContext,
    message: Message,
    command_args: Optional[str],
    tg_message_id: Optional[int] = None,
) -> str:
```

```python
async def add_internal_note(
    *,
    bot: Bot,
    context: OperatorTopicContext,
    message: Message,
    command_args: Optional[str],
    tg_message_id: Optional[int] = None,
) -> str:
```

In `commands.py`, pass `tg_message_id=message.message_id` when calling these functions.

### 4b: New functions — edit_operator_message, delete_operator_message

Add at the end of `operator_replies.py`:

```python
async def edit_operator_message(
    *,
    context: OperatorTopicContext,
    telegram_message_id: int,
    new_text: str,
) -> str:
    """Called when operator edits a Telegram message that was sent to HDE."""
    record = await db.get_sent_message(telegram_message_id, context.topic_id)
    if record is None:
        raise OperatorReplyError("Это сообщение не связано с HDE")

    if not new_text.strip():
        raise OperatorReplyError("Текст не может быть пустым")

    client = HDEApiClient()
    try:
        if record.entity_type == "post":
            await client.update_post(record.ticket_id, record.hde_entity_id, new_text)
        else:
            await client.update_comment(record.ticket_id, record.hde_entity_id, new_text)
    except HDEApiError as exc:
        raise OperatorReplyError(str(exc)) from exc

    ticket = await db.get_topic(record.ticket_id)
    display_id = ticket.unique_id if ticket else record.ticket_id
    return format_message_edited(display_id)


async def delete_operator_message(
    *,
    context: OperatorTopicContext,
    telegram_message_id: int,
) -> str:
    """Called when operator uses /delete replying to a message sent to HDE."""
    record = await db.get_sent_message(telegram_message_id, context.topic_id)
    if record is None:
        raise OperatorReplyError("Это сообщение не связано с HDE")

    client = HDEApiClient()
    try:
        if record.entity_type == "post":
            await client.delete_post(record.ticket_id, record.hde_entity_id)
        else:
            await client.delete_comment(record.ticket_id, record.hde_entity_id)
    except HDEApiError as exc:
        raise OperatorReplyError(str(exc)) from exc

    await db.delete_sent_message(telegram_message_id, context.topic_id)

    ticket = await db.get_topic(record.ticket_id)
    display_id = ticket.unique_id if ticket else record.ticket_id
    return format_message_deleted(display_id)
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_operator_replies.py`:

```python
@pytest.mark.asyncio
async def test_edit_operator_message_updates_hde_post(active_topic, initialized_db, monkeypatch):
    await db_module.save_sent_message(
        telegram_message_id=501,
        topic_id=9001,
        ticket_id="TKT-100",
        hde_entity_id=77,
        entity_type="post",
    )
    calls = []

    async def fake_update_post(self, ticket_id, post_id, text):
        calls.append(("update_post", ticket_id, post_id, text))

    monkeypatch.setattr("bot.hde_api.HDEApiClient.update_post", fake_update_post)

    result = await edit_operator_message(
        context=active_topic,
        telegram_message_id=501,
        new_text="corrected text",
    )

    assert "обновлено" in result
    assert calls == [("update_post", "TKT-100", 77, "corrected text")]


@pytest.mark.asyncio
async def test_edit_operator_message_raises_if_not_found(active_topic, initialized_db):
    with pytest.raises(OperatorReplyError, match="не связано с HDE"):
        await edit_operator_message(
            context=active_topic,
            telegram_message_id=999,
            new_text="anything",
        )


@pytest.mark.asyncio
async def test_delete_operator_message_deletes_hde_post(active_topic, initialized_db, monkeypatch):
    await db_module.save_sent_message(
        telegram_message_id=502,
        topic_id=9001,
        ticket_id="TKT-100",
        hde_entity_id=88,
        entity_type="post",
    )
    calls = []

    async def fake_delete_post(self, ticket_id, post_id):
        calls.append(("delete_post", ticket_id, post_id))

    monkeypatch.setattr("bot.hde_api.HDEApiClient.delete_post", fake_delete_post)

    result = await delete_operator_message(
        context=active_topic,
        telegram_message_id=502,
    )

    assert "удалено" in result
    assert calls == [("delete_post", "TKT-100", 88)]
    # mapping removed from DB
    assert await db_module.get_sent_message(502, 9001) is None


@pytest.mark.asyncio
async def test_delete_operator_message_raises_if_not_found(active_topic, initialized_db):
    with pytest.raises(OperatorReplyError, match="не связано с HDE"):
        await delete_operator_message(
            context=active_topic,
            telegram_message_id=999,
        )
```

Add to the imports at the top of `tests/test_operator_replies.py`:

```python
from bot.operator_replies import (
    ...,
    edit_operator_message,
    delete_operator_message,
)
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_operator_replies.py::test_edit_operator_message_updates_hde_post tests/test_operator_replies.py::test_delete_operator_message_deletes_hde_post -v
```

Expected: `ImportError: cannot import name 'edit_operator_message'`

- [ ] **Step 3: Implement** — make all the changes to `bot/operator_replies.py` described above

- [ ] **Step 4: Run to verify they pass**

```
pytest tests/test_operator_replies.py::test_edit_operator_message_updates_hde_post tests/test_operator_replies.py::test_edit_operator_message_raises_if_not_found tests/test_operator_replies.py::test_delete_operator_message_deletes_hde_post tests/test_operator_replies.py::test_delete_operator_message_raises_if_not_found -v
```

Expected: `4 passed`

- [ ] **Step 5: Run full suite**

```
pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git add bot/operator_replies.py tests/test_operator_replies.py
git commit -m "feat: save HDE message IDs and add edit/delete operator message functions"
```

---

## Task 5: handlers/commands.py — edited_message and /delete

**Files:**
- Modify: `bot/handlers/commands.py`

### 5a: Pass tg_message_id in existing /send and /note handlers

Update `cmd_note` to pass `tg_message_id`:

```python
@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        return await add_internal_note(
            bot=message.bot,
            context=context,
            message=message,
            command_args=command.args,
            tg_message_id=message.message_id,
        )
    await _run_operator_command(message, action)
```

Update `cmd_send` the same way — add `tg_message_id=message.message_id`.

### 5b: Add /delete command

Add after the existing `/send` handler:

```python
@router.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        reply = message.reply_to_message
        if reply is None:
            raise OperatorReplyError("Ответьте командой /delete на сообщение, которое хотите удалить из HDE")
        return await delete_operator_message(
            context=context,
            telegram_message_id=reply.message_id,
        )

    await _run_operator_command(message, action)
```

### 5c: Add edited_message handler

```python
from aiogram import F

@router.edited_message(F.message_thread_id.is_not(None))
async def on_edited_message(message: Message) -> None:
    """When operator edits a message in a topic, sync the edit to HDE."""
    if message.from_user is None or message.from_user.is_bot:
        return

    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        new_text = (message.text or message.caption or "").strip()
        return await edit_operator_message(
            context=context,
            telegram_message_id=message.message_id,
            new_text=new_text,
        )

    try:
        result = await action()
    except OperatorReplyError:
        # Silently ignore — most edits in topics are unrelated to HDE
        return
    except Exception:
        return

    await message.answer(result, parse_mode="HTML")
```

Add to the imports at the top of `commands.py`:

```python
from ..operator_replies import (
    add_internal_note,
    cache_incoming_topic_media,
    delete_operator_message,
    edit_operator_message,
    format_operator_exception,
    get_operator_topic_context,
    send_public_reply,
)
from ..formatter import OperatorReplyError  # OperatorReplyError is in operator_replies
```

Actually `OperatorReplyError` is in `operator_replies`, not `formatter`. Correct import:

```python
from ..operator_replies import (
    OperatorReplyError,
    add_internal_note,
    cache_incoming_topic_media,
    delete_operator_message,
    edit_operator_message,
    format_operator_exception,
    get_operator_topic_context,
    send_public_reply,
)
```

Also add `from aiogram import F, Router` (F may not be imported yet).

- [ ] **Step 1: Implement** — make all changes to `bot/handlers/commands.py`

(No separate test for command handlers — the logic is tested via operator_replies tests. The handler is thin wiring.)

- [ ] **Step 2: Update /help text** — add `/delete` to the help message in `cmd_help`:

```python
"/delete в ответ на сообщение — удалить его из HDE"
```

- [ ] **Step 3: Run full suite**

```
pytest tests/ -q
```

Expected: all pass

- [ ] **Step 4: Manual smoke test**

```
python start_dev.py
```

1. In a topic, send `/send Тестовый ответ` → бот отвечает "✅ Публичный ответ отправлен в HDE"
2. Отредактируй то сообщение в Telegram → бот отвечает "✏️ Сообщение обновлено в HDE"
3. Ответь на то же сообщение командой `/delete` → бот отвечает "🗑️ Сообщение удалено из HDE"
4. Проверь в интерфейсе HDE что изменения отразились

- [ ] **Step 5: Commit**

```bash
git add bot/handlers/commands.py
git commit -m "feat: add /delete command and edited_message handler for HDE sync"
```

---

## Summary of user-facing changes

| Action in Telegram | Effect in HDE |
|--------------------|---------------|
| Отредактировать сообщение в topic (отправленное через `/send` или `/note`) | PUT — текст обновляется в ответе/комментарии |
| `/delete` в ответ на сообщение (отправленное через `/send` или `/note`) | DELETE — запись удаляется из HDE |

**Ограничения:**
- Редактирование медиа (замена фото/файла) не поддерживается HDE API — только текст
- Telegram не уведомляет ботов об удалении сообщений, поэтому удаление в HDE всегда через команду `/delete`
- Если сообщение не было отправлено через `/send`/`/note`, обработчик молча игнорирует событие
