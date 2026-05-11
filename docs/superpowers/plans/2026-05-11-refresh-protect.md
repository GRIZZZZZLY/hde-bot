# Refresh Protection + Formatter Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent /refresh from deleting Telegram topics unless HDE confirms the ticket is closed/resolved; remove `{company_name}` from deletion summary and show a direct HDE link instead.

**Architecture:** Three-layer change — new `get_ticket_open_status` HDE API method (fail-safe, returns `None` on error); guard in `refresh.py` Step 2 that skips deletion when the method returns falsy; `format_refresh_result` updated to accept `list[tuple[str, str, str]]` for deleted items.

**Tech Stack:** Python 3.11+, aiohttp, pytest, pytest-asyncio, aiogram

---

## File Map

| File | Change |
|------|--------|
| `bot/hde_api.py` | Add `get_ticket_open_status` after `get_ticket_info` (line ~139) |
| `bot/refresh.py` | Step 2: verify before delete; `RefreshResult.deleted` type → `list[tuple[str,str,str]]` |
| `bot/formatter.py` | Update `format_refresh_result` deleted section (lines 389–394) |
| `tests/test_hde_payload.py` | Add 3 tests for `get_ticket_open_status`; update existing formatter test |
| `tests/test_refresh.py` | New file: Step 2 protection tests (3 cases) |

---

### Task 1: `get_ticket_open_status` method in hde_api.py

**Files:**
- Modify: `bot/hde_api.py` (insert after line 139)
- Test: `tests/test_hde_payload.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_hde_payload.py`:

```python
# --- new imports at top of file ---
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from bot.hde_api import HDEApiClient


@pytest.mark.asyncio
async def test_get_ticket_open_status_closed(monkeypatch):
    async def fake_read_response(self, response):
        return {"data": {"status": "closed", "link_staff": "https://hde.example.com/t/1"}}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=mock_resp)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("bot.hde_api.HDEApiClient._read_response", fake_read_response)

    with patch("aiohttp.ClientSession", return_value=mock_sess):
        client = HDEApiClient()
        result = await client.get_ticket_open_status("123")

    assert result == (True, "https://hde.example.com/t/1")


@pytest.mark.asyncio
async def test_get_ticket_open_status_open(monkeypatch):
    async def fake_read_response(self, response):
        return {"data": {"status": "open", "link_staff": "https://hde.example.com/t/2"}}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=mock_resp)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("bot.hde_api.HDEApiClient._read_response", fake_read_response)

    with patch("aiohttp.ClientSession", return_value=mock_sess):
        client = HDEApiClient()
        result = await client.get_ticket_open_status("456")

    assert result == (False, "https://hde.example.com/t/2")


@pytest.mark.asyncio
async def test_get_ticket_open_status_api_error(monkeypatch):
    async def fake_read_response(self, response):
        raise RuntimeError("network error")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=mock_resp)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("bot.hde_api.HDEApiClient._read_response", fake_read_response)

    with patch("aiohttp.ClientSession", return_value=mock_sess):
        client = HDEApiClient()
        result = await client.get_ticket_open_status("789")

    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd d:/HDE_bot && python -m pytest tests/test_hde_payload.py::test_get_ticket_open_status_closed tests/test_hde_payload.py::test_get_ticket_open_status_open tests/test_hde_payload.py::test_get_ticket_open_status_api_error -v
```

Expected: 3 failures — `AttributeError: HDEApiClient has no attribute get_ticket_open_status`

- [ ] **Step 3: Implement `get_ticket_open_status` in `bot/hde_api.py`**

Insert after `get_ticket_info` (after line 139, before `get_client_tickets`):

```python
    async def get_ticket_open_status(self, ticket_id: str) -> tuple[bool, str] | None:
        """Return (is_deletable, link_staff) or None on any error (fail-safe).

        is_deletable=True means status in {resolved, closed} → safe to delete topic.
        Returns None on network/API error → caller must skip deletion.
        """
        url = f"{self.base_url}/tickets/{ticket_id}/"
        try:
            async with aiohttp.ClientSession(auth=self.auth) as session:
                async with session.get(url) as response:
                    data = await self._read_response(response)
                    if response.status >= 400:
                        return None
            raw = data.get("data", data) if isinstance(data, dict) else {}
            status = raw.get("status", "")
            link = raw.get("link_staff", "")
            is_deletable = status in {"resolved", "closed"}
            return (is_deletable, link)
        except Exception:
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd d:/HDE_bot && python -m pytest tests/test_hde_payload.py::test_get_ticket_open_status_closed tests/test_hde_payload.py::test_get_ticket_open_status_open tests/test_hde_payload.py::test_get_ticket_open_status_api_error -v
```

Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add bot/hde_api.py tests/test_hde_payload.py
git commit -m "feat(hde_api): add get_ticket_open_status for refresh protection"
```

---

### Task 2: Update `format_refresh_result` deleted section

**Files:**
- Modify: `bot/formatter.py` (lines 389–394)
- Test: `tests/test_hde_payload.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_hde_payload.py`:

```python
def test_format_refresh_result_deleted_shows_link():
    text = format_refresh_result(
        active_count=2,
        hde_count=3,
        deleted=[("Сломан принтер", "42", "https://hde.example.com/t/42")],
    )
    assert "Удалено устаревших" in text
    assert "Сломан принтер" in text
    assert "https://hde.example.com/t/42" in text
    assert "Открыть в HDE" in text
    assert "company_name" not in text


def test_format_refresh_result_deleted_no_link():
    text = format_refresh_result(
        active_count=2,
        hde_count=3,
        deleted=[("Тикет без ссылки", "99", "")],
    )
    assert "Тикет без ссылки" in text
    assert "Открыть в HDE" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd d:/HDE_bot && python -m pytest tests/test_hde_payload.py::test_format_refresh_result_deleted_shows_link tests/test_hde_payload.py::test_format_refresh_result_deleted_no_link -v
```

Expected: 2 failures (current code treats deleted items as TicketTopic objects)

- [ ] **Step 3: Update `format_refresh_result` in `bot/formatter.py`**

Replace the deleted section (lines 389–394):

```python
    if deleted:
        lines.append(f"🗑️ Удалено устаревших: <b>{len(deleted)}</b>")
        for item in deleted:
            if isinstance(item, tuple):
                name, _ticket_id, link = item
                name = _escape(name)
                if link:
                    lines.append(f"  • {name} · <a href=\"{link}\">Открыть в HDE</a>")
                else:
                    lines.append(f"  • {name}")
            else:
                # legacy db.TicketTopic path (backward compat for direct callers)
                name = _escape(getattr(item, "ticket_name", "") or getattr(item, "ticket_id", ""))
                lines.append(f"  • {name}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd d:/HDE_bot && python -m pytest tests/test_hde_payload.py -v
```

Expected: all PASSED (including existing `test_format_refresh_result_no_changes`)

- [ ] **Step 5: Commit**

```bash
git add bot/formatter.py tests/test_hde_payload.py
git commit -m "feat(formatter): show HDE link instead of company in deleted topics"
```

---

### Task 3: Protect Step 2 in refresh.py

**Files:**
- Modify: `bot/refresh.py` (lines 20–28 dataclass + lines 155–177 Step 2)
- Test: `tests/test_refresh.py` (new file)

- [ ] **Step 1: Write failing tests**

Create `tests/test_refresh.py`:

```python
from __future__ import annotations

import pytest
from types import SimpleNamespace

import bot.db as db_module
from bot.refresh import RefreshResult, refresh_topics


class DummyBot:
    def __init__(self):
        self.deleted_topics: list[int] = []
        self.edited_topics: list[int] = []

    async def edit_forum_topic(self, chat_id, message_thread_id, **kwargs):
        self.edited_topics.append(message_thread_id)

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self.deleted_topics.append(message_thread_id)

    async def create_forum_topic(self, chat_id, name, **kwargs):
        return SimpleNamespace(message_thread_id=9999)

    async def send_message(self, *args, **kwargs):
        pass

    async def pin_message(self, *args, **kwargs):
        pass


def make_topic(**kwargs):
    defaults = dict(
        ticket_id="1", unique_id="TST-1", ticket_name="Test ticket",
        company_name="ACME", topic_id=100, topic_state="active",
        priority="medium", pre_sla_notify_at=None, pre_sla_message_id=None,
        pre_sla_sent_at=None, last_client_reply_at=None, reassurance_sent_at=None,
        deleted_at=None,
    )
    defaults.update(kwargs)
    return db_module.TicketTopic(**defaults)


@pytest.mark.asyncio
async def test_refresh_skips_deletion_when_api_error(monkeypatch):
    """API error (None) → topic not deleted (fail-safe)."""
    topic = make_topic(ticket_id="1", topic_id=100, topic_state="active")

    monkeypatch.setattr("bot.db.list_active_topics", lambda: _async([topic]))
    monkeypatch.setattr("bot.db.list_topics_by_state", lambda state: _async([]))
    monkeypatch.setattr("bot.db.count_active_topics", lambda: _async(1))
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", _async_method([]))
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_ticket_open_status", _async_method(None))

    bot = DummyBot()
    result = await refresh_topics(bot)

    assert 100 not in bot.deleted_topics
    assert result.deleted == []


@pytest.mark.asyncio
async def test_refresh_skips_deletion_when_ticket_still_open(monkeypatch):
    """is_deletable=False (ticket open) → topic not deleted."""
    topic = make_topic(ticket_id="2", topic_id=200, topic_state="active")

    monkeypatch.setattr("bot.db.list_active_topics", lambda: _async([topic]))
    monkeypatch.setattr("bot.db.list_topics_by_state", lambda state: _async([]))
    monkeypatch.setattr("bot.db.count_active_topics", lambda: _async(1))
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", _async_method([]))
    monkeypatch.setattr(
        "bot.hde_api.HDEApiClient.get_ticket_open_status",
        _async_method((False, "https://hde.example.com/t/2")),
    )

    bot = DummyBot()
    result = await refresh_topics(bot)

    assert 200 not in bot.deleted_topics
    assert result.deleted == []


@pytest.mark.asyncio
async def test_refresh_deletes_when_ticket_closed(monkeypatch):
    """is_deletable=True (ticket closed) → topic deleted + included in result."""
    topic = make_topic(ticket_id="3", topic_id=300, topic_state="active", ticket_name="Done ticket")

    monkeypatch.setattr("bot.db.list_active_topics", lambda: _async([topic]))
    monkeypatch.setattr("bot.db.list_topics_by_state", lambda state: _async([]))
    monkeypatch.setattr("bot.db.count_active_topics", lambda: _async(0))
    monkeypatch.setattr("bot.db.update_topic", _update_topic_noop)
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", _async_method([]))
    monkeypatch.setattr(
        "bot.hde_api.HDEApiClient.get_ticket_open_status",
        _async_method((True, "https://hde.example.com/t/3")),
    )

    bot = DummyBot()
    result = await refresh_topics(bot)

    assert 300 in bot.deleted_topics
    assert len(result.deleted) == 1
    name, ticket_id, link = result.deleted[0]
    assert name == "Done ticket"
    assert ticket_id == "3"
    assert link == "https://hde.example.com/t/3"


# ── helpers ──────────────────────────────────────────────────────────────────

async def _async(value):
    return value


def _async_method(value):
    async def method(self, *args, **kwargs):
        return value
    return method


async def _update_topic_noop(ticket_id, **kwargs):
    pass
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd d:/HDE_bot && python -m pytest tests/test_refresh.py -v
```

Expected: 3 failures — `get_ticket_open_status` not called / deletion not guarded

- [ ] **Step 3: Update `RefreshResult.deleted` type in `bot/refresh.py`**

In the `RefreshResult` dataclass, update the `deleted` field annotation:

```python
@dataclass
class RefreshResult:
    active_before: int
    hde_count: int
    created: list = field(default_factory=list)
    renamed: list = field(default_factory=list)
    deleted: list[tuple[str, str, str]] = field(default_factory=list)  # (ticket_name, ticket_id, link)
    cleaned_pending: int = 0
    active_after: int = 0
```

- [ ] **Step 4: Update Step 2 in `bot/refresh.py`**

Replace the Step 2 block (lines 155–177):

```python
    # ── Step 2: delete active topics whose tickets are gone from HDE ──────────
    deleted: list[tuple[str, str, str]] = []  # (ticket_name, ticket_id, link)
    for topic in active_topics:
        in_hde = (
            topic.ticket_id in hde_by_ticket_id
            or topic.unique_id in hde_by_unique_id
            or topic.ticket_id in hde_by_unique_id
        )
        if not in_hde:
            verify = await client.get_ticket_open_status(topic.ticket_id)
            if verify is None:
                logger.warning(
                    "refresh: skip deletion for ticket %s (HDE verify failed, fail-safe)",
                    topic.ticket_id,
                )
                continue
            is_deletable, link = verify
            if not is_deletable:
                logger.info(
                    "refresh: skip deletion for ticket %s (status not closed/resolved)",
                    topic.ticket_id,
                )
                continue
            await db.update_topic(
                topic.ticket_id,
                topic_state="deleted",
                deleted_at=to_storage(utcnow()),
            )
            try:
                await bot.delete_forum_topic(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic.topic_id,
                )
            except TelegramAPIError as exc:
                logger.warning("refresh: could not delete topic %d: %s", topic.topic_id, exc)
            deleted.append((topic.ticket_name or topic.ticket_id, topic.ticket_id, link))
            logger.info("refresh: deleted stale topic %d for ticket %s", topic.topic_id, topic.ticket_id)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd d:/HDE_bot && python -m pytest tests/test_refresh.py tests/test_hde_payload.py -v
```

Expected: all PASSED

- [ ] **Step 6: Run full test suite to check for regressions**

```bash
cd d:/HDE_bot && python -m pytest --tb=short -q
```

Expected: same failures as baseline (3 pre-existing failures: `test_scheduler_sends_pre_sla_alert`, `test_build_system_prompt_contains_role`, `test_find_similar_company_boost`). No new failures.

- [ ] **Step 7: Commit**

```bash
git add bot/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): verify HDE status before deleting topic (fail-safe guard)"
```
