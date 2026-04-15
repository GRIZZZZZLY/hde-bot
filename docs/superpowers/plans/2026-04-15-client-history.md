# Client History in Topic — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a new Telegram topic is created for a ticket, send an additional message showing the client's past tickets fetched from HDE API.

**Architecture:** Three changes to existing files — new `get_client_tickets()` method in `hde_api.py`, new `format_client_history()` in `formatter.py`, new `_post_client_history()` helper called at the end of `_post_ticket_history()` in `topic_manager.py`. Errors are swallowed so topic creation never fails.

**Tech Stack:** Python 3.10+, aiohttp, aiogram 3.x, pytest-asyncio, unittest.mock

---

## File Map

| File | Change |
|---|---|
| `bot/hde_api.py` | Add `get_client_tickets(client_id, limit)` method |
| `bot/formatter.py` | Add `format_client_history(client_name, total, recent_titles, last_date)` |
| `bot/topic_manager.py` | Add `_relative_date()` helper + `_post_client_history()` + wire call |
| `tests/test_client_history.py` | New test file covering all three units |

---

### Task 1: `get_client_tickets` in hde_api.py

**Files:**
- Modify: `bot/hde_api.py` (after `get_ticket_info`, around line 140)
- Test: `tests/test_client_history.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_client_history.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_get_client_tickets_returns_list():
    """get_client_tickets returns list of raw ticket dicts, newest first, limited."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)
    mock_response.json = AsyncMock(return_value={
        "data": [
            {"id": 10, "subject": "Тикет 10", "status": "closed", "date_created": "2026-04-10 10:00"},
            {"id": 9,  "subject": "Тикет 9",  "status": "closed", "date_created": "2026-04-09 10:00"},
            {"id": 8,  "subject": "Тикет 8",  "status": "closed", "date_created": "2026-04-08 10:00"},
        ]
    })

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_response)

    with patch("bot.hde_api.aiohttp.ClientSession", return_value=mock_session):
        from bot.hde_api import HDEApiClient
        client = object.__new__(HDEApiClient)
        client.base_url = "https://example.com/api/v2"
        client.auth = None

        result = await client.get_client_tickets(client_id=42, limit=10)

    assert len(result) == 3
    assert result[0]["id"] == 10
    assert result[0]["subject"] == "Тикет 10"


@pytest.mark.asyncio
async def test_get_client_tickets_respects_limit():
    """get_client_tickets slices result to limit."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)
    mock_response.json = AsyncMock(return_value={
        "data": [{"id": i, "subject": f"T{i}", "status": "closed", "date_created": ""} for i in range(20)]
    })

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_response)

    with patch("bot.hde_api.aiohttp.ClientSession", return_value=mock_session):
        from bot.hde_api import HDEApiClient
        client = object.__new__(HDEApiClient)
        client.base_url = "https://example.com/api/v2"
        client.auth = None

        result = await client.get_client_tickets(client_id=42, limit=5)

    assert len(result) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd d:/HDE_bot
python -m pytest tests/test_client_history.py -v
```

Expected: `AttributeError: 'HDEApiClient' object has no attribute 'get_client_tickets'`

- [ ] **Step 3: Implement `get_client_tickets` in `bot/hde_api.py`**

Add after `get_ticket_info` (after line ~139):

```python
async def get_client_tickets(self, client_id: int, limit: int = 10) -> list[dict]:
    """Return up to `limit` tickets for the given client (requester), newest first.

    Returns raw ticket dicts with keys: id, subject, status, date_created.
    """
    url = f"{self.base_url}/tickets/"
    params = {
        "user_list": str(client_id),
        "order_by": "id",
        "order_dir": "desc",
        "page": "1",
    }
    async with aiohttp.ClientSession(auth=self.auth) as session:
        async with session.get(url, params=params) as response:
            data = await self._read_response(response)
            if response.status >= 400:
                raise HDEApiError(
                    self._extract_error_message(data) or f"HDE API error {response.status}"
                )
    items = data.get("data", []) if isinstance(data, dict) else []
    return [
        {
            "id": item.get("id"),
            "subject": item.get("subject") or item.get("name") or "",
            "status": item.get("status", ""),
            "date_created": item.get("date_created", ""),
        }
        for item in items
        if isinstance(item, dict)
    ][:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_client_history.py::test_get_client_tickets_returns_list tests/test_client_history.py::test_get_client_tickets_respects_limit -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/hde_api.py tests/test_client_history.py
git commit -m "feat: add get_client_tickets to HDEApiClient"
```

---

### Task 2: `format_client_history` in formatter.py

**Files:**
- Modify: `bot/formatter.py` (append at end of file)
- Test: `tests/test_client_history.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_client_history.py`:

```python
def test_format_client_history_basic():
    from bot.formatter import format_client_history
    result = format_client_history(
        client_name="Мария Иванова",
        total=7,
        recent_titles=["Не работает TouchScreen", "Сбросились настройки", "TeamViewer"],
        last_ticket_date="3 дня назад",
    )
    assert "Мария Иванова" in result
    assert "7 обращений" in result
    assert "Не работает TouchScreen" in result
    assert "TeamViewer" in result
    assert "3 дня назад" in result


def test_format_client_history_empty_returns_empty():
    from bot.formatter import format_client_history
    result = format_client_history(
        client_name="Иван",
        total=0,
        recent_titles=[],
        last_ticket_date=None,
    )
    assert result == ""


def test_format_client_history_single_ticket():
    from bot.formatter import format_client_history
    result = format_client_history(
        client_name="Петр",
        total=1,
        recent_titles=["Принтер не печатает"],
        last_ticket_date="сегодня",
    )
    assert "1 обращение" in result or "1 обращений" in result
    assert "Принтер не печатает" in result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_client_history.py::test_format_client_history_basic tests/test_client_history.py::test_format_client_history_empty_returns_empty -v
```

Expected: `ImportError` — `format_client_history` does not exist yet.

- [ ] **Step 3: Implement `format_client_history` in `bot/formatter.py`**

Append to the end of `bot/formatter.py`:

```python
def format_client_history(
    client_name: str,
    total: int,
    recent_titles: list[str],
    last_ticket_date: Optional[str],
) -> str:
    """Format client past-tickets summary for display in a topic.

    Returns empty string when total == 0 (caller should skip sending).
    """
    if total == 0:
        return ""

    titles_block = "\n".join(f"• {_escape(t)}" for t in recent_titles) if recent_titles else ""
    last_line = f"\n🕐 Последнее: {_escape(last_ticket_date)}" if last_ticket_date else ""

    return (
        f"🏢 <b>Клиент: {_escape(client_name)}</b> — {total} обращений\n\n"
        f"📋 Последние темы:\n{titles_block}"
        f"{last_line}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_client_history.py::test_format_client_history_basic tests/test_client_history.py::test_format_client_history_empty_returns_empty tests/test_client_history.py::test_format_client_history_single_ticket -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/formatter.py tests/test_client_history.py
git commit -m "feat: add format_client_history to formatter"
```

---

### Task 3: `_post_client_history` in topic_manager.py

**Files:**
- Modify: `bot/topic_manager.py`
- Test: `tests/test_client_history.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_client_history.py`:

```python
@pytest.mark.asyncio
async def test_post_client_history_sends_message():
    """_post_client_history sends formatted message to topic when past tickets exist."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    mock_info = MagicMock()
    mock_info.client_id = 42
    mock_info.client_name = "Мария Иванова"

    mock_tickets = [
        {"id": 5, "subject": "Принтер", "status": "closed", "date_created": "2026-04-10 10:00"},
        {"id": 4, "subject": "TeamViewer", "status": "closed", "date_created": "2026-04-09 10:00"},
        {"id": 3, "subject": "Настройки", "status": "closed", "date_created": "2026-04-08 10:00"},
    ]

    with patch("bot.topic_manager.hde_api.get_ticket_info", AsyncMock(return_value=mock_info)), \
         patch("bot.topic_manager.hde_api.get_client_tickets", AsyncMock(return_value=mock_tickets)):
        from bot.topic_manager import _post_client_history
        await _post_client_history(mock_bot, topic_id=101, ticket_id="999")

    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert "Мария Иванова" in call_kwargs["text"]
    assert "3 обращений" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_post_client_history_skips_when_no_past_tickets():
    """_post_client_history sends nothing if all tickets are the current one."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    mock_info = MagicMock()
    mock_info.client_id = 42
    mock_info.client_name = "Петр"

    # Only the current ticket in results
    mock_tickets = [{"id": 999, "subject": "Current", "status": "open", "date_created": ""}]

    with patch("bot.topic_manager.hde_api.get_ticket_info", AsyncMock(return_value=mock_info)), \
         patch("bot.topic_manager.hde_api.get_client_tickets", AsyncMock(return_value=mock_tickets)):
        from bot.topic_manager import _post_client_history
        await _post_client_history(mock_bot, topic_id=101, ticket_id="999")

    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_post_client_history_swallows_api_error():
    """_post_client_history does not raise when HDE API fails."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with patch("bot.topic_manager.hde_api.get_ticket_info", AsyncMock(side_effect=Exception("HDE down"))):
        from bot.topic_manager import _post_client_history
        await _post_client_history(mock_bot, topic_id=101, ticket_id="999")  # must not raise

    mock_bot.send_message.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_client_history.py::test_post_client_history_sends_message tests/test_client_history.py::test_post_client_history_skips_when_no_past_tickets tests/test_client_history.py::test_post_client_history_swallows_api_error -v
```

Expected: `ImportError` — `_post_client_history` does not exist yet.

- [ ] **Step 3: Add `_relative_date` helper and `_post_client_history` to `bot/topic_manager.py`**

Add `_relative_date` near the other small helpers (after `_parse_minutes`, around line 95):

```python
def _relative_date(date_str: str | None) -> str | None:
    """Convert HDE date string to Russian relative label.

    Handles formats: 'YYYY-MM-DD HH:MM:SS', 'DD.MM.YYYY HH:MM', ISO 8601.
    Returns None if date_str is empty or unparseable.
    """
    if not date_str:
        return None
    formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d"]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str[:19], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None

    today = utcnow().date()
    delta = (today - dt.date()).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    if delta < 7:
        return f"{delta} дня назад" if delta in (2, 3, 4) else f"{delta} дней назад"
    if delta < 30:
        weeks = delta // 7
        return f"{weeks} нед. назад"
    months = delta // 30
    return f"{months} мес. назад"
```

Add `_post_client_history` near `send_pre_sla_alert` (end of file, before `delete_pending_topic`):

```python
async def _post_client_history(bot: Bot, topic_id: int, ticket_id: str) -> None:
    """Fetch client's past tickets from HDE and post a summary to the topic.

    Silently skips on any error or if no past tickets exist.
    """
    try:
        info = await hde_api.get_ticket_info(ticket_id)
        if not info or not info.client_id:
            return
        tickets = await hde_api.get_client_tickets(info.client_id, limit=10)
        past = [t for t in tickets if str(t.get("id", "")) != str(ticket_id)]
        if not past:
            return
        recent_titles = [t["subject"] for t in past[:5] if t.get("subject")]
        last_date = _relative_date(past[0].get("date_created"))
        text = format_client_history(
            client_name=info.client_name,
            total=len(past),
            recent_titles=recent_titles,
            last_ticket_date=last_date,
        )
        if text:
            await _send_topic_message(bot, topic_id, text)
    except Exception as exc:
        logger.warning("Client history failed for ticket %s: %s", ticket_id, exc)
```

Also add `format_client_history` to the existing import from `.formatter` at the top of `topic_manager.py`:

```python
from .formatter import (
    format_assignment_message,
    format_client_history,       # ← add this line
    format_client_reply,
    ...
)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_client_history.py::test_post_client_history_sends_message tests/test_client_history.py::test_post_client_history_skips_when_no_past_tickets tests/test_client_history.py::test_post_client_history_swallows_api_error -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/topic_manager.py tests/test_client_history.py
git commit -m "feat: add _post_client_history helper to topic_manager"
```

---

### Task 4: Wire into `_post_ticket_history`

**Files:**
- Modify: `bot/topic_manager.py` (line ~255, end of `_post_ticket_history`)
- Test: `tests/test_client_history.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_client_history.py`:

```python
@pytest.mark.asyncio
async def test_post_ticket_history_calls_client_history():
    """_post_ticket_history calls _post_client_history at the end."""
    from unittest.mock import AsyncMock, patch, MagicMock
    import bot.topic_manager as tm

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with patch.object(tm, "_post_client_history", AsyncMock()) as mock_ch, \
         patch("bot.topic_manager.hde_api.get_ticket_posts", AsyncMock(return_value=[])), \
         patch("bot.topic_manager.hde_api.get_ticket_comments", AsyncMock(return_value=[])):
        await tm._post_ticket_history(mock_bot, ticket_id="123", topic_id=101)

    mock_ch.assert_called_once_with(mock_bot, 101, "123")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_client_history.py::test_post_ticket_history_calls_client_history -v
```

Expected: FAIL — `_post_client_history` is never called.

- [ ] **Step 3: Add the call at the end of `_post_ticket_history`**

Read `bot/topic_manager.py` around line 255 to find the end of `_post_ticket_history`. Add one line before the closing of the function:

```python
    # After posting all ticket history messages:
    await _post_client_history(bot, topic_id, ticket_id)
```

- [ ] **Step 4: Run all client history tests**

```bash
python -m pytest tests/test_client_history.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: No new failures.

- [ ] **Step 6: Commit**

```bash
git add bot/topic_manager.py tests/test_client_history.py
git commit -m "feat: show client ticket history when topic is created"
```

- [ ] **Step 7: Push**

```bash
git push
```
