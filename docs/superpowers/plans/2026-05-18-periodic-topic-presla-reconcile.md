# Periodic Personal-Topic + Pre-SLA Reconcile — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a throttled periodic scheduler job that creates missing forum topics for the operator's open HDE tickets and arms the pre-SLA timer for unanswered client messages, alerting the operator when a lost webhook had to be recovered.

**Architecture:** One new async function `reconcile_personal_topics(bot)` in `bot/refresh.py` reusing `HDEApiClient`, `topic_manager.sync_ticket_topic`, `topic_manager._schedule_pre_sla`. Wired into the existing scheduler tick via a new `_maybe_reconcile_personal(bot)` wrapper with a ~10-minute throttle modelled on `_maybe_reconcile_general`. Pure additive overlay — webhook path and `/refresh` unchanged.

**Tech Stack:** Python 3, aiogram, aiohttp, pytest + pytest-asyncio, sqlite (via `bot.db`).

Spec: `docs/superpowers/specs/2026-05-18-periodic-topic-presla-reconcile-design.md`

---

## File Structure

- `bot/refresh.py` — MODIFY. Add: `PersonalReconcileResult` dataclass, helpers `_hde_post_to_storage`, `_last_post_is_client`, `_send_operator_alert`, and `reconcile_personal_topics`. New imports.
- `bot/scheduler.py` — MODIFY. Add throttle globals `_last_personal_reconcile_at` / `_PERSONAL_RECONCILE_INTERVAL_SEC`, wrapper `_maybe_reconcile_personal`, one call in `process_scheduled_actions`.
- `tests/test_refresh.py` — MODIFY. Add tests for the new function + helpers.

Confirmed patterns from the codebase:
- HDE post date format `"%H:%M:%S %d.%m.%Y"`, interpreted as **MSK**, converted to UTC (see `topic_manager.py:1273-1277`).
- `_schedule_pre_sla(ticket_id, payload, last_client_reply_at)` → `_calculate_pre_sla_notify_at` reads `payload["last_post_date"]` (parsed by `time_utils.parse_datetime`, which accepts storage format `"%Y-%m-%d %H:%M:%S"`).
- `get_ticket_posts` returns posts **oldest-first**; `HDEPost` has `user_id`, `is_comment`, `date_created`.
- Staff vs client: post is staff when `post.user_id == int(config.hde_owner_id)` (same rule as `_hde_staff_replied_since`).
- `config.operator_telegram_user_ids: tuple[int, ...]` — DM target for the alert.
- Test helpers already in `tests/test_refresh.py`: `DummyBot`, `make_topic`, `_async`, `_noop_update_topic`.

---

## Task 1: Pure helpers — HDE post date conversion + last-post-is-client

**Files:**
- Modify: `bot/refresh.py`
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_refresh.py`:

```python
from bot.refresh import _hde_post_to_storage, _last_post_is_client
from bot.hde_api import HDEPost


def test_hde_post_to_storage_converts_msk_to_utc():
    # 12:00:00 MSK on 18.05.2026 == 09:00:00 UTC
    assert _hde_post_to_storage("12:00:00 18.05.2026") == "2026-05-18 09:00:00"


def test_hde_post_to_storage_bad_input_returns_none():
    assert _hde_post_to_storage("not-a-date") is None
    assert _hde_post_to_storage("") is None


def test_last_post_is_client_true_when_newest_noncomment_is_client():
    posts = [  # oldest-first, as get_ticket_posts returns
        HDEPost(post_id=1, user_id=98, text="staff", date_created="10:00:00 18.05.2026"),
        HDEPost(post_id=2, user_id=500, text="client", date_created="11:00:00 18.05.2026"),
    ]
    assert _last_post_is_client(posts, owner_id=98) is True


def test_last_post_is_client_false_when_newest_is_staff():
    posts = [
        HDEPost(post_id=1, user_id=500, text="client", date_created="10:00:00 18.05.2026"),
        HDEPost(post_id=2, user_id=98, text="staff", date_created="11:00:00 18.05.2026"),
    ]
    assert _last_post_is_client(posts, owner_id=98) is False


def test_last_post_is_client_ignores_internal_comments():
    posts = [
        HDEPost(post_id=1, user_id=500, text="client", date_created="10:00:00 18.05.2026"),
        HDEPost(post_id=2, user_id=98, text="note", date_created="11:00:00 18.05.2026", is_comment=True),
    ]
    assert _last_post_is_client(posts, owner_id=98) is True


def test_last_post_is_client_false_when_no_posts():
    assert _last_post_is_client([], owner_id=98) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k "hde_post_to_storage or last_post_is_client" -v`
Expected: FAIL with `ImportError: cannot import name '_hde_post_to_storage'`

- [ ] **Step 3: Implement the helpers**

In `bot/refresh.py`, update the imports block (currently lines 1-17) to add `datetime`/`timezone`/`timedelta` and `HDEPost`:

```python
# refresh.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config
from .formatter import make_topic_name
from .hde_api import HDEApiClient, HDEApiError, HDEPost, HDETicket
from .time_utils import to_storage, utcnow
from .topic_manager import sync_ticket_topic

logger = logging.getLogger(__name__)

_MSK_TZ = timezone(timedelta(hours=3))
```

Then add the two helpers immediately after `_topic_display_name` (after current line 57):

```python
def _hde_post_to_storage(date_created: str) -> str | None:
    """Convert an HDE post date 'HH:MM:SS DD.MM.YYYY' (MSK) to UTC storage form.

    Returns None if the string cannot be parsed.
    """
    try:
        dt = datetime.strptime(date_created, "%H:%M:%S %d.%m.%Y")
    except (ValueError, TypeError):
        return None
    return to_storage(dt.replace(tzinfo=_MSK_TZ).astimezone(timezone.utc))


def _last_post_is_client(posts: list[HDEPost], owner_id: int) -> bool:
    """True if the most recent non-comment post is from someone other than the operator.

    `posts` is oldest-first (as get_ticket_posts returns). Internal comments are
    ignored. No qualifying post → False (do not treat as an unanswered client).
    """
    for post in reversed(posts):
        if post.is_comment:
            continue
        return post.user_id != owner_id
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k "hde_post_to_storage or last_post_is_client" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): add HDE post-date + last-post-is-client helpers"
```

---

## Task 2: `PersonalReconcileResult` + topic recovery loop

**Files:**
- Modify: `bot/refresh.py`
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_refresh.py`:

```python
import bot.refresh as refresh_module
from bot.hde_api import HDETicket


def _make_hde_ticket(ticket_id="200", unique_id="EQ-200"):
    return HDETicket(
        ticket_id=ticket_id,
        unique_id=unique_id,
        title="Kassa broken",
        company_name="ACME",
        owner_id="98",
        sla_date=None,
        hde_link="https://hde/t/200",
        link_staff="https://hde/t/200",
    )


class _StubHDEClient:
    def __init__(self, tickets, posts=None, raise_tickets=False, raise_posts=False):
        self._tickets = tickets
        self._posts = posts or []
        self._raise_tickets = raise_tickets
        self._raise_posts = raise_posts

    async def get_my_open_tickets(self):
        if self._raise_tickets:
            raise HDEApiError("boom")
        return self._tickets

    async def get_ticket_posts(self, ticket_id, limit=20):
        if self._raise_posts:
            raise HDEApiError("posts boom")
        return self._posts


@pytest.mark.asyncio
async def test_reconcile_creates_missing_topic(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="200")
    synced: list[str] = []

    monkeypatch.setattr(refresh_module, "HDEApiClient", lambda: _StubHDEClient([ticket]))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(None))

    async def _sync(bot, payload):
        synced.append(payload["ticket_id"])
    monkeypatch.setattr(refresh_module, "sync_ticket_topic", _sync)
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    result = await refresh_module.reconcile_personal_topics(DummyBot())

    assert synced == ["200"]
    assert "200" in result.recovered_topics
    assert result.recovered == 1


@pytest.mark.asyncio
async def test_reconcile_skips_existing_active_topic(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="201")
    existing = make_topic(ticket_id="201", topic_id=300,
                          pre_sla_notify_at="2026-05-18 10:00:00")
    synced: list[str] = []

    monkeypatch.setattr(refresh_module, "HDEApiClient", lambda: _StubHDEClient([ticket]))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(existing))

    async def _sync(bot, payload):
        synced.append(payload["ticket_id"])
    monkeypatch.setattr(refresh_module, "sync_ticket_topic", _sync)
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    result = await refresh_module.reconcile_personal_topics(DummyBot())

    assert synced == []          # topic exists → not re-synced
    assert result.recovered == 0


@pytest.mark.asyncio
async def test_reconcile_skips_pending_delete(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="202")
    pending = make_topic(ticket_id="202", topic_id=301,
                         topic_state="pending_delete")
    synced: list[str] = []

    monkeypatch.setattr(refresh_module, "HDEApiClient", lambda: _StubHDEClient([ticket]))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(pending))

    async def _sync(bot, payload):
        synced.append(payload["ticket_id"])
    monkeypatch.setattr(refresh_module, "sync_ticket_topic", _sync)
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    result = await refresh_module.reconcile_personal_topics(DummyBot())

    assert synced == []
    assert result.recovered == 0


@pytest.mark.asyncio
async def test_reconcile_hde_error_propagates(monkeypatch):
    monkeypatch.setattr(refresh_module, "HDEApiClient",
                        lambda: _StubHDEClient([], raise_tickets=True))
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    with pytest.raises(HDEApiError):
        await refresh_module.reconcile_personal_topics(DummyBot())
```

> **Note on `make_topic`:** `topic_state="pending_delete"` must yield `record.is_pending_delete == True`. `make_topic` builds a `db.TicketTopic`; confirm `is_pending_delete` is derived from `topic_state`. If `db.TicketTopic` has no such property, use the actual attribute the codebase exposes (grep `is_pending_delete` in `bot/db.py`) and adjust the guard in Step 3 to match. Do not invent a property.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k reconcile -v`
Expected: FAIL with `AttributeError: module 'bot.refresh' has no attribute 'reconcile_personal_topics'`

- [ ] **Step 3: Implement the dataclass and function (topic recovery only; pre-SLA in Task 3)**

Add after the `RefreshResult` dataclass (after current line 28):

```python
@dataclass
class PersonalReconcileResult:
    recovered_topics: list = field(default_factory=list)  # ticket_ids with a created topic
    armed_presla: list = field(default_factory=list)      # ticket_ids with a newly armed timer

    @property
    def recovered(self) -> int:
        return len(set(self.recovered_topics) | set(self.armed_presla))
```

Add at the end of `bot/refresh.py`:

```python
async def reconcile_personal_topics(bot: Bot) -> PersonalReconcileResult:
    """Periodic safety net for lost assignment/client_reply webhooks.

    Creates missing topics for the operator's open HDE tickets. Pre-SLA arming
    and the operator alert are added in later tasks.

    Raises HDEApiError if the ticket list cannot be fetched (caller must not
    advance its throttle, so the next tick retries).
    """
    result = PersonalReconcileResult()
    client = HDEApiClient()
    tickets = await client.get_my_open_tickets()  # HDEApiError propagates

    owner_id_str = config.hde_owner_id.strip()
    try:
        owner_id = int(owner_id_str) if owner_id_str else None
    except ValueError:
        owner_id = None

    for ticket in tickets:
        record = await db.get_topic(ticket.ticket_id)

        if record is not None and (record.is_pending_delete or record.is_deleted):
            continue

        if record is None:
            await sync_ticket_topic(bot, _ticket_to_payload(ticket, None))
            result.recovered_topics.append(ticket.ticket_id)
            record = await db.get_topic(ticket.ticket_id)
            if record is None:
                continue

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k reconcile -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): reconcile_personal_topics creates missing topics"
```

---

## Task 3: Pre-SLA arming inside the reconcile loop

**Files:**
- Modify: `bot/refresh.py`
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_refresh.py`:

```python
@pytest.mark.asyncio
async def test_reconcile_arms_presla_when_last_post_client(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="210")
    record = make_topic(ticket_id="210", topic_id=400,
                        pre_sla_notify_at=None, pre_sla_sent_at=None)
    posts = [HDEPost(post_id=1, user_id=500, text="help",
                     date_created="12:00:00 18.05.2026")]
    scheduled: list[tuple] = []

    monkeypatch.setattr(refresh_module, "HDEApiClient",
                        lambda: _StubHDEClient([ticket], posts=posts))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(record))
    monkeypatch.setattr(refresh_module, "sync_ticket_topic",
                        lambda bot, payload: _async(None))

    async def _sched(ticket_id, payload, last_client_reply_at):
        scheduled.append((ticket_id, payload["last_post_date"], last_client_reply_at))
    monkeypatch.setattr(refresh_module, "_schedule_pre_sla", _sched)
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    result = await refresh_module.reconcile_personal_topics(DummyBot())

    assert scheduled == [("210", "2026-05-18 09:00:00", "2026-05-18 09:00:00")]
    assert "210" in result.armed_presla
    assert result.recovered == 1


@pytest.mark.asyncio
async def test_reconcile_skips_presla_when_last_post_staff(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="211")
    record = make_topic(ticket_id="211", topic_id=401)
    posts = [HDEPost(post_id=1, user_id=98, text="answered",
                     date_created="12:00:00 18.05.2026")]
    scheduled: list = []

    monkeypatch.setattr(refresh_module, "HDEApiClient",
                        lambda: _StubHDEClient([ticket], posts=posts))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(record))
    monkeypatch.setattr(refresh_module, "sync_ticket_topic",
                        lambda bot, payload: _async(None))
    monkeypatch.setattr(refresh_module, "_schedule_pre_sla",
                        lambda *a, **k: scheduled.append(a) or _async(None))
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    result = await refresh_module.reconcile_personal_topics(DummyBot())

    assert scheduled == []
    assert result.recovered == 0


@pytest.mark.asyncio
async def test_reconcile_skips_presla_when_already_armed(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="212")
    record = make_topic(ticket_id="212", topic_id=402,
                        pre_sla_notify_at="2026-05-18 10:00:00")
    scheduled: list = []

    monkeypatch.setattr(refresh_module, "HDEApiClient",
                        lambda: _StubHDEClient([ticket], posts=[]))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(record))
    monkeypatch.setattr(refresh_module, "sync_ticket_topic",
                        lambda bot, payload: _async(None))
    monkeypatch.setattr(refresh_module, "_schedule_pre_sla",
                        lambda *a, **k: scheduled.append(a) or _async(None))
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    result = await refresh_module.reconcile_personal_topics(DummyBot())

    assert scheduled == []
    assert result.recovered == 0


@pytest.mark.asyncio
async def test_reconcile_skips_presla_when_already_sent(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="213")
    record = make_topic(ticket_id="213", topic_id=403,
                        pre_sla_notify_at=None,
                        pre_sla_sent_at="2026-05-18 09:30:00")
    scheduled: list = []

    monkeypatch.setattr(refresh_module, "HDEApiClient",
                        lambda: _StubHDEClient([ticket], posts=[]))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(record))
    monkeypatch.setattr(refresh_module, "sync_ticket_topic",
                        lambda bot, payload: _async(None))
    monkeypatch.setattr(refresh_module, "_schedule_pre_sla",
                        lambda *a, **k: scheduled.append(a) or _async(None))
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    result = await refresh_module.reconcile_personal_topics(DummyBot())

    assert scheduled == []
    assert result.recovered == 0


@pytest.mark.asyncio
async def test_reconcile_posts_error_skips_one_ticket(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="214")
    record = make_topic(ticket_id="214", topic_id=404,
                        pre_sla_notify_at=None, pre_sla_sent_at=None)
    scheduled: list = []

    monkeypatch.setattr(refresh_module, "HDEApiClient",
                        lambda: _StubHDEClient([ticket], raise_posts=True))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(record))
    monkeypatch.setattr(refresh_module, "sync_ticket_topic",
                        lambda bot, payload: _async(None))
    monkeypatch.setattr(refresh_module, "_schedule_pre_sla",
                        lambda *a, **k: scheduled.append(a) or _async(None))
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)

    result = await refresh_module.reconcile_personal_topics(DummyBot())

    assert scheduled == []          # posts failed → timer skipped
    assert result.recovered == 0    # no exception, loop survived
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k "reconcile and presla" -v`
Expected: FAIL — `_schedule_pre_sla` not imported / not called

- [ ] **Step 3: Implement pre-SLA arming**

In `bot/refresh.py`, add `_schedule_pre_sla` to the topic_manager import (replace the existing line `from .topic_manager import sync_ticket_topic`):

```python
from .topic_manager import _schedule_pre_sla, sync_ticket_topic
```

In `reconcile_personal_topics`, replace the `for ticket in tickets:` loop body so that, after the topic-recovery block, it arms the timer. The full loop becomes:

```python
    for ticket in tickets:
        record = await db.get_topic(ticket.ticket_id)

        if record is not None and (record.is_pending_delete or record.is_deleted):
            continue

        if record is None:
            await sync_ticket_topic(bot, _ticket_to_payload(ticket, None))
            result.recovered_topics.append(ticket.ticket_id)
            record = await db.get_topic(ticket.ticket_id)
            if record is None:
                continue

        # Arm pre-SLA only when no timer exists and none was ever sent.
        if record.pre_sla_notify_at is not None or record.pre_sla_sent_at is not None:
            continue
        if owner_id is None:
            continue
        try:
            posts = await client.get_ticket_posts(ticket.ticket_id, limit=5)
        except HDEApiError as exc:
            logger.warning(
                "Personal reconcile: posts fetch failed for %s: %s",
                ticket.ticket_id, exc,
            )
            continue
        if not _last_post_is_client(posts, owner_id):
            continue
        last_client_post = next(
            (p for p in reversed(posts) if not p.is_comment), None
        )
        if last_client_post is None:
            continue
        storage_date = _hde_post_to_storage(last_client_post.date_created)
        if storage_date is None:
            continue
        payload = _ticket_to_payload(ticket, record)
        payload["last_post_date"] = storage_date
        await _schedule_pre_sla(ticket.ticket_id, payload, storage_date)
        result.armed_presla.append(ticket.ticket_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k reconcile -v`
Expected: PASS (all reconcile tests, 9)

- [ ] **Step 5: Commit**

```bash
git add bot/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): arm pre-SLA in reconcile when last post is client"
```

---

## Task 4: Operator alert when recovery happened

**Files:**
- Modify: `bot/refresh.py`
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_refresh.py`:

```python
class _CapturingBot(DummyBot):
    def __init__(self):
        super().__init__()
        self.messages: list[tuple] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_reconcile_alert_sent_when_recovered(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="220")

    monkeypatch.setattr(refresh_module, "HDEApiClient",
                        lambda: _StubHDEClient([ticket]))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(None))
    monkeypatch.setattr(refresh_module, "sync_ticket_topic",
                        lambda bot, payload: _async(None))
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)
    monkeypatch.setattr(config, "operator_telegram_user_ids", (111, 222),
                        raising=False)

    bot = _CapturingBot()
    result = await refresh_module.reconcile_personal_topics(bot)

    assert result.recovered == 1
    assert [m[0] for m in bot.messages] == [111, 222]
    assert "#220" in bot.messages[0][1]


@pytest.mark.asyncio
async def test_reconcile_no_alert_when_nothing_recovered(monkeypatch):
    ticket = _make_hde_ticket(ticket_id="221")
    existing = make_topic(ticket_id="221", topic_id=500,
                          pre_sla_notify_at="2026-05-18 10:00:00")

    monkeypatch.setattr(refresh_module, "HDEApiClient",
                        lambda: _StubHDEClient([ticket]))
    monkeypatch.setattr("bot.db.get_topic", lambda tid: _async(existing))
    monkeypatch.setattr(refresh_module, "sync_ticket_topic",
                        lambda bot, payload: _async(None))
    monkeypatch.setattr(config, "hde_owner_id", "98", raising=False)
    monkeypatch.setattr(config, "operator_telegram_user_ids", (111,),
                        raising=False)

    bot = _CapturingBot()
    result = await refresh_module.reconcile_personal_topics(bot)

    assert result.recovered == 0
    assert bot.messages == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k "alert" -v`
Expected: FAIL — no message sent (alert not implemented)

- [ ] **Step 3: Implement the alert**

Add this helper at the end of `bot/refresh.py`, before `reconcile_personal_topics`:

```python
async def _send_operator_alert(bot: Bot, result: PersonalReconcileResult) -> None:
    ids = sorted(set(result.recovered_topics) | set(result.armed_presla))
    text = (
        "⚠️ <b>Webhook recovery</b>\n"
        f"Восстановлено тикетов: {len(ids)}\n"
        + ", ".join(f"#{tid}" for tid in ids)
    )
    for uid in config.operator_telegram_user_ids:
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except TelegramAPIError as exc:
            logger.warning(
                "Personal reconcile: alert to %s failed: %s", uid, exc
            )
```

At the end of `reconcile_personal_topics`, before `return result`, add:

```python
    if result.recovered > 0:
        await _send_operator_alert(bot, result)
    return result
```

(Replace the existing bare `return result` with the two lines above.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k reconcile -v`
Expected: PASS (all reconcile tests, 11)

- [ ] **Step 5: Commit**

```bash
git add bot/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): DM operator when webhook recovery occurred"
```

---

## Task 5: Scheduler wiring with ~10-minute throttle

**Files:**
- Modify: `bot/scheduler.py`
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_refresh.py`:

```python
import bot.scheduler as scheduler_module


@pytest.mark.asyncio
async def test_maybe_reconcile_personal_runs_and_throttles(monkeypatch):
    calls: list[int] = []

    monkeypatch.setattr(scheduler_module, "_last_personal_reconcile_at", None,
                        raising=False)
    monkeypatch.setattr("bot.work_schedule.is_work_time", lambda: True)

    async def _fake_reconcile(bot):
        calls.append(1)
    monkeypatch.setattr("bot.refresh.reconcile_personal_topics", _fake_reconcile)

    bot = DummyBot()
    await scheduler_module._maybe_reconcile_personal(bot)  # runs
    await scheduler_module._maybe_reconcile_personal(bot)  # throttled

    assert calls == [1]


@pytest.mark.asyncio
async def test_maybe_reconcile_personal_skips_outside_work_time(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(scheduler_module, "_last_personal_reconcile_at", None,
                        raising=False)
    monkeypatch.setattr("bot.work_schedule.is_work_time", lambda: False)

    async def _fake_reconcile(bot):
        calls.append(1)
    monkeypatch.setattr("bot.refresh.reconcile_personal_topics", _fake_reconcile)

    await scheduler_module._maybe_reconcile_personal(DummyBot())
    assert calls == []


@pytest.mark.asyncio
async def test_maybe_reconcile_personal_does_not_advance_throttle_on_error(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(scheduler_module, "_last_personal_reconcile_at", None,
                        raising=False)
    monkeypatch.setattr("bot.work_schedule.is_work_time", lambda: True)

    async def _boom(bot):
        calls.append(1)
        raise RuntimeError("hde down")
    monkeypatch.setattr("bot.refresh.reconcile_personal_topics", _boom)

    bot = DummyBot()
    await scheduler_module._maybe_reconcile_personal(bot)  # error
    await scheduler_module._maybe_reconcile_personal(bot)  # retried (not throttled)

    assert calls == [1, 1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k maybe_reconcile_personal -v`
Expected: FAIL with `AttributeError: module 'bot.scheduler' has no attribute '_maybe_reconcile_personal'`

- [ ] **Step 3: Implement scheduler wiring**

In `bot/scheduler.py`, after the existing line `_GENERAL_RECONCILE_INTERVAL_SEC = 7 * 60` (line 41), add:

```python
_last_personal_reconcile_at: Optional[datetime] = None  # last personal-topic reconcile
_PERSONAL_RECONCILE_INTERVAL_SEC = 10 * 60
```

Add this wrapper immediately after `_maybe_reconcile_general` (after current line 121):

```python
async def _maybe_reconcile_personal(bot: Bot) -> None:
    """Periodic safety net: recover topics/pre-SLA for the operator's tickets
    when an assignment/client_reply webhook was lost. Work-hours only.

    The throttle timestamp is advanced ONLY on success, so a failed cycle is
    retried on the next tick.
    """
    global _last_personal_reconcile_at
    from .work_schedule import is_work_time
    if not is_work_time():
        return
    now = datetime.now(timezone.utc)
    if (
        _last_personal_reconcile_at is not None
        and (now - _last_personal_reconcile_at).total_seconds() < _PERSONAL_RECONCILE_INTERVAL_SEC
    ):
        return
    from .refresh import reconcile_personal_topics
    try:
        await reconcile_personal_topics(bot)
    except Exception as exc:
        logger.warning("Periodic personal reconcile failed: %s", exc)
        return  # throttle NOT advanced → retry next tick
    _last_personal_reconcile_at = now
```

In `process_scheduled_actions`, immediately after the existing line `await _maybe_reconcile_general(bot)` (line 268), add:

```python
    await _maybe_reconcile_personal(bot)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py -k maybe_reconcile_personal -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full refresh + scheduler test suite**

Run: `cd d:/HDE_bot && python -m pytest tests/test_refresh.py tests/test_topic_manager.py -v`
Expected: PASS (all existing + new tests, no regressions)

- [ ] **Step 6: Commit**

```bash
git add bot/scheduler.py tests/test_refresh.py
git commit -m "feat(scheduler): wire periodic personal-topic reconcile (10m throttle)"
```

---

## Task 6: Syntax check, deploy, live verification

**Files:** none (deploy + observe)

- [ ] **Step 1: Compile check**

Run: `cd d:/HDE_bot && python -m py_compile bot/refresh.py bot/scheduler.py && echo "SYNTAX OK"`
Expected: `SYNTAX OK`

- [ ] **Step 2: Full test suite**

Run: `cd d:/HDE_bot && python -m pytest -q`
Expected: all pass (no regressions across the suite)

- [ ] **Step 3: Deploy** (push triggers VPS pull + restart per the deploy pipeline)

```bash
git push origin main
ssh config1 "cd /opt/hde-bot && git pull && sudo systemctl restart hde-bot && sleep 2 && sudo systemctl is-active hde-bot"
```
Expected: `active`

- [ ] **Step 4: Live verification**

Within ~10 min of a work-hours tick, check the new job ran:

```bash
ssh config1 "sudo journalctl -u hde-bot --since '15 min ago' --no-pager | grep -iE 'personal reconcile|Webhook recovery'"
```
Expected: either no output (nothing to recover — healthy) or a recovery line + an operator DM. No tracebacks.

Manual end-to-end (optional): assign a test ticket to the operator in HDE while the bot's webhook receiver is paused/unreachable; within ~10 min the topic should appear and, if the client had the last word, a pre-SLA timer should be set; the operator receives the ⚠️ DM.

---

## Self-Review

**Spec coverage:**
- Goal 1 (create missing topics) → Task 2.
- Goal 2 (arm pre-SLA on unanswered client message) → Task 1 (helpers) + Task 3.
- Goal 3 (operator alert) → Task 4.
- Periodic + ~10-min throttle, work-hours, fail-safe (throttle not advanced on error) → Task 5.
- Edge cases: pending_delete/deleted skip (Task 2), already-armed/already-sent (Task 3), posts error skips one ticket (Task 3), bot's own reassurance = operator user_id = staff (covered by `_last_post_is_client` staff rule, Task 1/3), empty posts (Task 1/3), HDE list error fail-safe (Task 2 raises + Task 5 no-throttle-advance).
- Post-date MSK→UTC conversion → Task 1.

**Placeholder scan:** No TBD/TODO. The only deferred item is the spec's reassurance-author verification — handled concretely: `_last_post_is_client` treats `user_id == hde_owner_id` as staff, and the Task 2 note instructs grepping `is_pending_delete` in `bot/db.py` to use the real property rather than inventing one.

**Type consistency:** `PersonalReconcileResult` (`recovered_topics`, `armed_presla`, `.recovered`) used identically across Tasks 2–4. `_hde_post_to_storage`, `_last_post_is_client`, `_send_operator_alert`, `reconcile_personal_topics`, `_maybe_reconcile_personal` names consistent across tasks and tests. `_schedule_pre_sla` signature `(ticket_id, payload, last_client_reply_at)` matches `topic_manager.py:750`.
