# Morning Digest + /refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) Send a morning digest to the General topic at 08:00 MSK with overnight assignments, open ticket count, and SLA-sorted queue. (2) Add `/refresh` command that syncs DB with HDE API and marks stale topics as deleted.

**Architecture:**
- Digest: Scheduler checks time every cycle; at 08:00 MSK (05:00 UTC) sends one digest per day. Overnight tickets come from `ticket_topics.last_assigned_at` (new column). SLA data comes from a fresh HDE API call (`GET /tickets/`). Digest goes to `GROUP_CHAT_ID` (General topic = no thread ID).
- Refresh: `/refresh` command calls HDE API for all open tickets assigned to operator, compares with active DB topics, marks missing ones as `deleted` and closes their Telegram topics.

**Tech Stack:** Python 3.12, aiogram 3.x, aiohttp, aiosqlite, pytest-asyncio

---

## File Map

| File | Change |
|------|--------|
| `bot/db.py` | Add `last_assigned_at` column; add `list_overnight_assigned()` and `list_active_topics()` queries |
| `bot/hde_api.py` | Add `get_my_open_tickets()` — paginated `GET /tickets/?owner_list=...` |
| `bot/topic_manager.py` | Set `last_assigned_at` when processing assignment events |
| `bot/formatter.py` | Add `format_morning_digest()` and `format_refresh_result()` |
| `bot/digest.py` | New file — `send_morning_digest(bot)` logic |
| `bot/refresh.py` | New file — `refresh_topics(bot)` logic, returns `RefreshResult` |
| `bot/scheduler.py` | Add digest scheduling (check time each cycle, fire once per day) |
| `bot/config.py` | Add `digest_send_hour_utc: int = 5` (08:00 MSK = 05:00 UTC), `digest_night_start_hour: int = 15` (18:00 MSK = 15:00 UTC) |
| `bot/handlers/commands.py` | Add `/refresh` command |

---

## Task 1: DB — `last_assigned_at` column and new queries

**Files:**
- Modify: `bot/db.py`
- Tests: `tests/test_topic_manager.py` (or new section in `test_operator_replies.py`)

### 1a: Add column to `TICKET_TOPIC_COLUMNS`

In `db.py`, add to `TICKET_TOPIC_COLUMNS` dict (alongside existing entries like `delete_after_at`):

```python
"last_assigned_at": "TEXT",
```

Add to `UPDATABLE_FIELDS` set:

```python
"last_assigned_at",
```

Add to `TicketTopic` dataclass (after `deleted_at`):

```python
last_assigned_at: Optional[str]
```

Update `_row_to_topic()` to include:

```python
last_assigned_at=row["last_assigned_at"],
```

### 1b: Add two query functions at the end of `db.py`

```python
async def list_overnight_assigned(night_start: str, night_end: str) -> list[TicketTopic]:
    """Return topics where last_assigned_at is between night_start and night_end (UTC strings)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM ticket_topics
            WHERE last_assigned_at >= ?
              AND last_assigned_at < ?
              AND topic_state != 'deleted'
            ORDER BY last_assigned_at ASC
            """,
            (night_start, night_end),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def list_active_topics() -> list[TicketTopic]:
    """Return all topics with topic_state = 'active'."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_topics WHERE topic_state = 'active' ORDER BY created_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]
```

### Steps

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_operator_replies.py`:

```python
# ── list_overnight_assigned + list_active_topics ───────────────────────────────

@pytest.mark.asyncio
async def test_list_overnight_assigned_returns_matching(initialized_db):
    await db_module.upsert_topic(
        "TKT-200", 9100,
        unique_id="ABC-200", company_name="ACME", ticket_name="Night ticket",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/200",
    )
    await db_module.update_topic("TKT-200", last_assigned_at="2026-04-04 02:00:00")

    results = await db_module.list_overnight_assigned("2026-04-03 15:00:00", "2026-04-04 05:00:00")
    assert any(r.ticket_id == "TKT-200" for r in results)


@pytest.mark.asyncio
async def test_list_overnight_assigned_excludes_outside_window(initialized_db):
    await db_module.upsert_topic(
        "TKT-201", 9101,
        unique_id="ABC-201", company_name="ACME", ticket_name="Day ticket",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/201",
    )
    await db_module.update_topic("TKT-201", last_assigned_at="2026-04-04 10:00:00")

    results = await db_module.list_overnight_assigned("2026-04-03 15:00:00", "2026-04-04 05:00:00")
    assert not any(r.ticket_id == "TKT-201" for r in results)


@pytest.mark.asyncio
async def test_list_active_topics_returns_active(initialized_db):
    await db_module.upsert_topic(
        "TKT-210", 9110,
        unique_id="ABC-210", company_name="ACME", ticket_name="Active",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/210",
    )
    results = await db_module.list_active_topics()
    assert any(r.ticket_id == "TKT-210" for r in results)


@pytest.mark.asyncio
async def test_list_active_topics_excludes_deleted(initialized_db):
    await db_module.upsert_topic(
        "TKT-211", 9111,
        unique_id="ABC-211", company_name="ACME", ticket_name="Deleted",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/211",
    )
    await db_module.update_topic("TKT-211", topic_state="deleted")
    results = await db_module.list_active_topics()
    assert not any(r.ticket_id == "TKT-211" for r in results)
```

- [ ] **Step 2: Run to verify they fail**

```
cd d:/HDE_bot && python -m pytest tests/test_operator_replies.py::test_list_overnight_assigned_returns_matching tests/test_operator_replies.py::test_list_active_topics_returns_active -v
```

Expected: `AttributeError: module 'bot.db' has no attribute 'list_overnight_assigned'`

- [ ] **Step 3: Implement** all changes to `bot/db.py`

- [ ] **Step 4: Run to verify they pass**

```
cd d:/HDE_bot && python -m pytest tests/test_operator_replies.py::test_list_overnight_assigned_returns_matching tests/test_operator_replies.py::test_list_overnight_assigned_excludes_outside_window tests/test_operator_replies.py::test_list_active_topics_returns_active tests/test_operator_replies.py::test_list_active_topics_excludes_deleted -v
```

Expected: `4 passed`

- [ ] **Step 5: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

---

## Task 2: HDE API — `get_my_open_tickets()`

**Files:**
- Modify: `bot/hde_api.py`
- Tests: `tests/test_operator_replies.py`

### What to add

Add dataclass for HDE ticket (lean — only what we need):

```python
@dataclass
class HDETicket:
    ticket_id: str        # numeric string id
    unique_id: str        # e.g. "ABC-123"
    title: str
    company_name: str     # from user_name field (client name)
    owner_id: str
    sla_date: Optional[str]   # "17.01.2017 16:00" format or None
    hde_link: str         # constructed from base_url + unique_id
    link_staff: str       # staff link if available
```

Add method to `HDEApiClient`:

```python
async def get_my_open_tickets(self) -> list[HDETicket]:
    """Return all open/in-progress tickets assigned to me, paginated."""
    all_tickets: list[HDETicket] = []
    page = 1
    owner_id = config.hde_owner_id

    while True:
        url = f"{self.base_url}/tickets/"
        params = {
            "owner_list": owner_id,
            "status_list": "open,process",
            "page": str(page),
        }
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.get(url, params=params) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)

        if not isinstance(data, dict):
            break
        tickets_data = data.get("data", {})
        if not tickets_data:
            break

        for ticket_raw in tickets_data.values():
            if not isinstance(ticket_raw, dict):
                continue
            ticket_id = str(ticket_raw.get("id", ""))
            unique_id = ticket_raw.get("unique_id", ticket_id)
            title = ticket_raw.get("title", "")
            # Client name: user_name + user_lastname
            user_name = ticket_raw.get("user_name", "")
            user_lastname = ticket_raw.get("user_lastname", "")
            company_name = f"{user_name} {user_lastname}".strip() or "—"
            sla_date = ticket_raw.get("sla_date") or None
            link_staff = f"{self.base_url.replace('/api/v2', '')}/tickets/{ticket_id}"
            all_tickets.append(HDETicket(
                ticket_id=ticket_id,
                unique_id=unique_id,
                title=title,
                company_name=company_name,
                owner_id=str(ticket_raw.get("owner_id", "")),
                sla_date=sla_date,
                hde_link=link_staff,
                link_staff=link_staff,
            ))

        # HDE API paginates — if fewer than expected, we're done
        # (HDE default page size is 25; stop when we get an empty page)
        meta = data.get("meta", {})
        total_pages = meta.get("total_pages", 1) if isinstance(meta, dict) else 1
        if page >= total_pages:
            break
        page += 1

    return all_tickets
```

### Steps

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_get_my_open_tickets_returns_list(monkeypatch):
    fake_response = {
        "data": {
            "10": {
                "id": 10,
                "unique_id": "ABC-010",
                "title": "Test ticket",
                "user_name": "John",
                "user_lastname": "Doe",
                "owner_id": 1,
                "sla_date": "05.04.2026 10:00",
            }
        },
        "meta": {"total_pages": 1},
    }

    async def fake_get(self, url, params=None):
        class FakeResp:
            status = 200
            headers = {"Content-Type": "application/json"}
            async def json(self_inner):
                return fake_response
            async def text(self_inner):
                import json
                return json.dumps(fake_response)
            async def __aenter__(self_inner):
                return self_inner
            async def __aexit__(self_inner, *args):
                pass
        return FakeResp()

    class FakeSession:
        def get(self, url, params=None):
            return fake_get(self, url, params)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("aiohttp.ClientSession", lambda **kwargs: FakeSession())
    client = HDEApiClient()
    tickets = await client.get_my_open_tickets()
    assert len(tickets) == 1
    assert tickets[0].unique_id == "ABC-010"
    assert tickets[0].title == "Test ticket"
    assert tickets[0].company_name == "John Doe"
    assert tickets[0].sla_date == "05.04.2026 10:00"
```

- [ ] **Step 2: Run to verify it fails**

```
cd d:/HDE_bot && python -m pytest tests/test_operator_replies.py::test_get_my_open_tickets_returns_list -v
```

- [ ] **Step 3: Implement** — add `HDETicket` dataclass and `get_my_open_tickets()` to `bot/hde_api.py`

- [ ] **Step 4: Run to verify it passes**

```
cd d:/HDE_bot && python -m pytest tests/test_operator_replies.py::test_get_my_open_tickets_returns_list -v
```

- [ ] **Step 5: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

---

## Task 3: topic_manager.py — set `last_assigned_at` on assignment

**Files:**
- Modify: `bot/topic_manager.py`

### What to change

When processing `ticket_assigned` events (and `assigned_on_create`), the code calls `db.upsert_topic(...)` or `db.update_topic(...)`. After those calls, add:

```python
await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
```

Look for the functions `handle_ticket_assigned` and `handle_assigned_on_create` (or however assignment is handled) in `topic_manager.py`. Find every place a topic is first created or re-assigned to the operator, and add the `last_assigned_at` update there.

### Steps

- [ ] **Step 1: Read `bot/topic_manager.py`** to find the exact assignment handling locations

- [ ] **Step 2: Add `last_assigned_at=to_storage(utcnow())` updates** in the assignment handlers

- [ ] **Step 3: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

Expected: all pass (existing tests cover topic creation)

---

## Task 4: Config — digest timing settings

**Files:**
- Modify: `bot/config.py`

### What to add

In the `Config` dataclass, add two fields:

```python
digest_send_hour_utc: int    # 5 = 08:00 MSK (UTC+3)
digest_night_start_hour_utc: int   # 15 = 18:00 MSK (UTC+3)
```

In `from_env()`, add to the constructor:

```python
digest_send_hour_utc=int(os.getenv("DIGEST_SEND_HOUR_UTC", "5")),
digest_night_start_hour_utc=int(os.getenv("DIGEST_NIGHT_START_HOUR_UTC", "15")),
```

### Steps

- [ ] **Step 1: Implement** — add the two fields to `Config` dataclass and `from_env()`

- [ ] **Step 2: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

Expected: all pass

---

## Task 5: Formatter — digest and refresh messages

**Files:**
- Modify: `bot/formatter.py`

### What to add

```python
from datetime import datetime, timezone
from typing import Optional

def _parse_hde_sla_date(sla_date: Optional[str]) -> Optional[datetime]:
    """Parse HDE sla_date format: '17.01.2017 16:00' (assumed Moscow time UTC+3)."""
    if not sla_date or sla_date == "null":
        return None
    from datetime import timezone as tz
    import zoneinfo
    try:
        msk = zoneinfo.ZoneInfo("Europe/Moscow")
        dt = datetime.strptime(sla_date, "%d.%m.%Y %H:%M")
        return dt.replace(tzinfo=msk).astimezone(timezone.utc)
    except (ValueError, KeyError):
        return None


def _sla_remaining_text(sla_date: Optional[str]) -> Optional[str]:
    """Return human-readable time until SLA, or None if no valid SLA."""
    dt = _parse_hde_sla_date(sla_date)
    if dt is None:
        return None
    from datetime import timezone
    now = datetime.now(timezone.utc)
    delta = dt - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return None  # already past — skip in digest
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}ч {minutes}мин" if minutes else f"{hours}ч"
    return f"{minutes}мин"
```

Note: `zoneinfo` is in Python 3.9+ stdlib. No new dependency needed.

```python
def format_morning_digest(
    night_start_label: str,   # e.g. "18:00 03.04"
    night_end_label: str,     # e.g. "08:00 04.04"
    assigned_tickets: list,   # list of TicketTopic from db
    total_open: int,
    open_tickets_with_sla: list,  # list of HDETicket sorted by sla_date asc
) -> str:
    lines = [
        f"📊 <b>Сводка за ночь</b>",
        f"🕕 {night_start_label} — {night_end_label}",
        "",
        f"📥 Назначено за ночь: <b>{len(assigned_tickets)}</b>",
        f"🟢 Открытых тикетов сейчас: <b>{total_open}</b>",
    ]

    if assigned_tickets:
        lines.append("")
        for t in assigned_tickets:
            ticket_line = f"• {_escape(t.ticket_name)} — {_escape(t.company_name)}"
            if t.hde_link:
                ticket_line += f' <a href="{_escape(t.hde_link)}">🔗</a>'
            lines.append(ticket_line)

    # SLA section — show tickets with upcoming SLA sorted ascending
    sla_lines = []
    for ticket in open_tickets_with_sla:
        remaining = _sla_remaining_text(ticket.sla_date)
        if remaining is None:
            continue
        line = f"• {_escape(ticket.title)} — {_escape(ticket.company_name)} — {remaining}"
        line += f' <a href="{_escape(ticket.link_staff)}">🔗</a>'
        sla_lines.append((ticket.sla_date, line))

    if sla_lines:
        lines.append("")
        lines.append("⏰ <b>Очередь по SLA:</b>")
        for _, line in sla_lines:
            lines.append(line)

    return "\n".join(lines)


def format_refresh_result(
    active_count: int,
    marked_deleted: list,   # list of TicketTopic that were marked deleted
    pending_delete_count: int,
) -> str:
    lines = [
        "🔄 <b>Синхронизация завершена</b>",
        "",
        f"✅ Активных топиков: <b>{active_count}</b>",
    ]
    if marked_deleted:
        lines.append(f"🗑️ Помечено удалёнными: <b>{len(marked_deleted)}</b>")
        for t in marked_deleted:
            name = _escape(t.ticket_name) or _escape(t.ticket_id)
            lines.append(f"  • {name} — {_escape(t.company_name)}")
    if pending_delete_count:
        lines.append(f"⏳ Ожидают удаления: <b>{pending_delete_count}</b>")
    if not marked_deleted:
        lines.append("✨ Всё актуально, расхождений нет")
    return "\n".join(lines)
```

### Steps

- [ ] **Step 1: Write the failing tests**

In `tests/test_hde_payload.py`:

```python
from bot.formatter import format_morning_digest, format_refresh_result

def test_format_morning_digest_empty_night():
    text = format_morning_digest(
        night_start_label="18:00 03.04",
        night_end_label="08:00 04.04",
        assigned_tickets=[],
        total_open=5,
        open_tickets_with_sla=[],
    )
    assert "Сводка за ночь" in text
    assert "Назначено за ночь: 0" in text
    assert "Открытых тикетов сейчас: 5" in text


def test_format_refresh_result_no_changes():
    text = format_refresh_result(active_count=3, marked_deleted=[], pending_delete_count=0)
    assert "Синхронизация завершена" in text
    assert "Активных топиков: 3" in text
    assert "расхождений нет" in text
```

- [ ] **Step 2: Run to verify they fail**

```
cd d:/HDE_bot && python -m pytest tests/test_hde_payload.py::test_format_morning_digest_empty_night tests/test_hde_payload.py::test_format_refresh_result_no_changes -v
```

- [ ] **Step 3: Implement** — add all new functions to `bot/formatter.py`

- [ ] **Step 4: Run to verify they pass**

```
cd d:/HDE_bot && python -m pytest tests/test_hde_payload.py::test_format_morning_digest_empty_night tests/test_hde_payload.py::test_format_refresh_result_no_changes -v
```

- [ ] **Step 5: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

---

## Task 6: `bot/digest.py` — morning digest logic

**Files:**
- Create: `bot/digest.py`

```python
# digest.py
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config
from .formatter import format_morning_digest
from .hde_api import HDEApiClient, HDEApiError
from .time_utils import to_storage

logger = logging.getLogger(__name__)


def _night_window(send_hour_utc: int, night_start_hour_utc: int) -> tuple[datetime, datetime]:
    """Calculate the overnight window ending now (at send_hour_utc today)."""
    now = datetime.now(timezone.utc)
    night_end = now.replace(hour=send_hour_utc, minute=0, second=0, microsecond=0)
    # If night_start is e.g. 15 (18:00 MSK) and send is 5 (08:00 MSK):
    # night started yesterday at night_start_hour_utc
    night_start = (night_end - timedelta(hours=1)).replace(
        hour=night_start_hour_utc, minute=0, second=0, microsecond=0
    )
    if night_start >= night_end:
        night_start -= timedelta(days=1)
    return night_start, night_end


def _label(dt: datetime) -> str:
    """Format datetime as 'HH:MM DD.MM' in MSK (+3)."""
    msk = dt + timedelta(hours=3)
    return msk.strftime("%H:%M %d.%m")


async def send_morning_digest(bot: Bot) -> None:
    night_start, night_end = _night_window(
        config.digest_send_hour_utc,
        config.digest_night_start_hour_utc,
    )

    assigned = await db.list_overnight_assigned(
        to_storage(night_start),
        to_storage(night_end),
    )

    active_topics = await db.list_active_topics()
    total_open = len(active_topics)

    # Fetch live SLA data from HDE
    open_tickets_with_sla: list = []
    try:
        client = HDEApiClient()
        all_tickets = await client.get_my_open_tickets()
        # Sort by sla_date ascending (None/missing go to end)
        def sla_sort_key(t):
            if not t.sla_date or t.sla_date == "null":
                return "9999-99-99"
            try:
                from datetime import datetime as dt
                parsed = dt.strptime(t.sla_date, "%d.%m.%Y %H:%M")
                return parsed.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                return "9999-99-99"
        open_tickets_with_sla = sorted(all_tickets, key=sla_sort_key)
    except HDEApiError as exc:
        logger.warning("Failed to fetch tickets for digest SLA: %s", exc)

    text = format_morning_digest(
        night_start_label=_label(night_start),
        night_end_label=_label(night_end),
        assigned_tickets=assigned,
        total_open=total_open,
        open_tickets_with_sla=open_tickets_with_sla,
    )

    try:
        await bot.send_message(
            chat_id=config.group_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Morning digest sent: %d assigned overnight, %d open", len(assigned), total_open)
    except TelegramAPIError as exc:
        logger.error("Failed to send morning digest: %s", exc)
```

### Steps

- [ ] **Step 1: Create `bot/digest.py`** with the content above

- [ ] **Step 2: Write a smoke test**

In `tests/test_operator_replies.py`:

```python
@pytest.mark.asyncio
async def test_send_morning_digest_sends_message(initialized_db, monkeypatch):
    sent = []

    async def fake_send_message(self, chat_id, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr("aiogram.Bot.send_message", fake_send_message)
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", lambda self: [])

    from bot.digest import send_morning_digest
    await send_morning_digest(bot=DummyBot())

    assert len(sent) == 1
    assert "Сводка за ночь" in sent[0]
```

- [ ] **Step 3: Run test**

```
cd d:/HDE_bot && python -m pytest tests/test_operator_replies.py::test_send_morning_digest_sends_message -v
```

- [ ] **Step 4: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

---

## Task 7: `bot/refresh.py` — sync DB with HDE API

**Files:**
- Create: `bot/refresh.py`

```python
# refresh.py
from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config
from .hde_api import HDEApiClient, HDEApiError
from .time_utils import to_storage, utcnow

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    active_before: int
    marked_deleted: list  # list of db.TicketTopic
    pending_delete_count: int
    active_after: int


async def refresh_topics(bot: Bot) -> RefreshResult:
    """
    Sync active topics in DB with HDE API.
    Tickets that are in DB as 'active' but NOT returned by HDE as open/in-progress
    get marked as 'deleted' and their Telegram topics are closed.
    """
    active_topics = await db.list_active_topics()
    active_before = len(active_topics)

    try:
        client = HDEApiClient()
        hde_tickets = await client.get_my_open_tickets()
    except HDEApiError as exc:
        logger.error("HDE API error during refresh: %s", exc)
        raise

    # Build set of ticket IDs that HDE says are open and assigned to me
    hde_ticket_ids = {t.ticket_id for t in hde_tickets}
    # Also match by unique_id (some topics may store unique_id as ticket_id)
    hde_unique_ids = {t.unique_id for t in hde_tickets}

    marked_deleted: list[db.TicketTopic] = []
    for topic in active_topics:
        # Consider it stale if neither its ticket_id nor unique_id is in HDE results
        in_hde = (
            topic.ticket_id in hde_ticket_ids
            or topic.unique_id in hde_unique_ids
            or topic.ticket_id in hde_unique_ids
        )
        if not in_hde:
            await db.update_topic(
                topic.ticket_id,
                topic_state="deleted",
                deleted_at=to_storage(utcnow()),
            )
            # Try to close the Telegram topic
            try:
                await bot.close_forum_topic(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic.topic_id,
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not close topic %s during refresh: %s", topic.topic_id, exc
                )
            marked_deleted.append(topic)

    # Count pending_delete topics
    all_topics = await db.list_active_topics()
    pending = await db.count_pending_delete_topics()

    return RefreshResult(
        active_before=active_before,
        marked_deleted=marked_deleted,
        pending_delete_count=pending,
        active_after=len(all_topics),
    )
```

### Steps

- [ ] **Step 1: Create `bot/refresh.py`** with the content above

- [ ] **Step 2: Write a test**

```python
@pytest.mark.asyncio
async def test_refresh_marks_stale_topic_deleted(initialized_db, monkeypatch):
    await db_module.upsert_topic(
        "TKT-300", 9200,
        unique_id="ABC-300", company_name="ACME", ticket_name="Stale",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/300",
    )

    # HDE returns empty — no open tickets assigned to me
    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", lambda self: [])

    async def fake_close_topic(self, chat_id, message_thread_id):
        pass
    monkeypatch.setattr("aiogram.Bot.close_forum_topic", fake_close_topic)

    from bot.refresh import refresh_topics
    result = await refresh_topics(bot=DummyBot())

    assert len(result.marked_deleted) == 1
    assert result.marked_deleted[0].ticket_id == "TKT-300"

    topic = await db_module.get_topic("TKT-300")
    assert topic.is_deleted
```

- [ ] **Step 3: Run test**

```
cd d:/HDE_bot && python -m pytest tests/test_operator_replies.py::test_refresh_marks_stale_topic_deleted -v
```

- [ ] **Step 4: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

---

## Task 8: Scheduler — daily digest trigger

**Files:**
- Modify: `bot/scheduler.py`

### What to add

The scheduler loop runs every `config.scheduler_interval_seconds`. Add a check: if current UTC hour == `config.digest_send_hour_utc` and we haven't sent today's digest yet, send it.

Track last digest date in memory (a module-level variable — no DB needed, restarts safely: at worst sends digest on restart if it happens at 08:00):

```python
# at module level
_last_digest_date: Optional[str] = None  # "YYYY-MM-DD" UTC


async def _maybe_send_digest(bot: Bot) -> None:
    global _last_digest_date
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour != config.digest_send_hour_utc:
        return
    if _last_digest_date == today:
        return
    _last_digest_date = today
    from .digest import send_morning_digest
    await send_morning_digest(bot)
```

In `process_scheduled_actions`, add call:

```python
await _maybe_send_digest(bot)
```

### Steps

- [ ] **Step 1: Implement** — add `_last_digest_date`, `_maybe_send_digest()`, and call in `process_scheduled_actions`

- [ ] **Step 2: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

---

## Task 9: `/refresh` command

**Files:**
- Modify: `bot/handlers/commands.py`

### What to add

Import at top:
```python
from ..refresh import refresh_topics
from ..formatter import format_refresh_result
```

Add handler after `/status`:

```python
@router.message(Command("refresh"))
async def cmd_refresh(message: Message) -> None:
    wait_msg = await message.answer("🔄 Синхронизирую с HDE...")
    try:
        result = await refresh_topics(bot=message.bot)
    except Exception as exc:
        await wait_msg.delete()
        await message.answer(f"⚠️ <b>Ошибка при синхронизации:</b> {exc}", parse_mode="HTML")
        return

    await wait_msg.delete()
    text = format_refresh_result(
        active_count=result.active_after,
        marked_deleted=result.marked_deleted,
        pending_delete_count=result.pending_delete_count,
    )
    await message.answer(text, parse_mode="HTML")
```

Update `/help` to include:
```python
"/refresh — синхронизировать топики с HDE, убрать устаревшие\n"
```

### Steps

- [ ] **Step 1: Implement** — add imports and `/refresh` handler to `bot/handlers/commands.py`, update `/help`

- [ ] **Step 2: Run full suite**

```
cd d:/HDE_bot && python -m pytest tests/ -q
```

Expected: all pass

---

## Final Verification

After all tasks:

```
cd d:/HDE_bot && python -m pytest tests/ -v
```

**Manual smoke tests:**

**Digest:**
1. In `.env` temporarily set `DIGEST_SEND_HOUR_UTC` to current UTC hour + 1 minute
2. Run `python start_dev.py`
3. Wait for scheduler cycle — digest should appear in General group topic

**Refresh:**
1. Manually delete a Telegram topic in the group
2. Run `/refresh` in personal chat
3. Bot should respond with that topic marked as deleted, count reduced

**Check `/status` after `/refresh`** — counts should now match reality.
