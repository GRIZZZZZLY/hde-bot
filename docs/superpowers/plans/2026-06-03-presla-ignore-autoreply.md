# Pre-SLA: ignore automated staff replies — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop automated staff replies (PosifloraSupportBot, HDE dispatcher echoes) from wiping the pre-SLA timer, so the safety net actually fires for unanswered clients.

**Architecture:** In the `staff_reply` webhook handler, clear the pre-SLA timer only when the *operator* genuinely replied — verified via the existing `_hde_staff_replied_since` HDE-API check (`post.user_id == hde_owner_id`). Falls back to the current timestamp comparison when HDE verification is disabled.

**Tech Stack:** Python 3, aiogram, aiohttp, aiosqlite, pytest (`pytest.mark.asyncio`), `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-06-03-presla-ignore-autoreply-design.md`

---

## File Structure

- Modify: `bot/topic_manager.py` — `_handle_staff_reply_locked` (~lines 1181–1185): replace the `should_clear` computation. `_hde_staff_replied_since` (same module, line 1304) and `config` (already imported) are reused as-is.
- Modify: `tests/test_topic_manager.py` — add 3 tests after `test_staff_reply_same_second_does_not_clear_pre_sla` (line 352).

No new files, no schema change, no new config (`presla_hde_verify` and `hde_owner_id` already exist).

---

## Task 1: Verify-operator gate before clearing the pre-SLA timer

**Files:**
- Modify: `bot/topic_manager.py:1181-1185`
- Test: `tests/test_topic_manager.py` (append after line 352)

- [ ] **Step 0: Create a feature branch** (repo is on `main`)

```bash
git checkout -b fix/presla-ignore-autoreply
```

- [ ] **Step 1: Write the three failing tests**

Append to `tests/test_topic_manager.py` (after `test_staff_reply_same_second_does_not_clear_pre_sla`). `AsyncMock`, `to_storage`, `utcnow`, `timedelta`, `topic_manager`, `db_module`, `make_payload`, `make_bot` are already imported/defined at the top of the file.

```python
@pytest.mark.asyncio
async def test_staff_reply_autoreply_does_not_clear_pre_sla(initialized_db, monkeypatch):
    """Automated staff_reply (PosifloraSupportBot / dispatcher echo) arrives
    newer than the client message but is NOT the operator. With HDE verify on,
    the timer must survive so pre-SLA can still fire (ticket 171623)."""
    notify = to_storage(utcnow() + timedelta(minutes=10))
    lcr = to_storage(utcnow() - timedelta(minutes=1))
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=notify, last_client_reply_at=lcr,
    )
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "me")
    verify = AsyncMock(return_value=False)  # operator has NOT posted in HDE
    monkeypatch.setattr(topic_manager, "_hde_staff_replied_since", verify)
    bot = make_bot()

    await handle_staff_reply(bot, make_payload(
        last_post_date=to_storage(utcnow()), user_name="PosifloraSupportBot"))

    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at == notify   # timer preserved
    verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_staff_reply_operator_reply_clears_pre_sla(initialized_db, monkeypatch):
    """A genuine operator reply (verified present in HDE) clears the timer."""
    notify = to_storage(utcnow() + timedelta(minutes=10))
    lcr = to_storage(utcnow() - timedelta(minutes=1))
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=notify, last_client_reply_at=lcr,
    )
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "me")
    verify = AsyncMock(return_value=True)  # operator genuinely replied
    monkeypatch.setattr(topic_manager, "_hde_staff_replied_since", verify)
    bot = make_bot()

    await handle_staff_reply(bot, make_payload(
        last_post_date=to_storage(utcnow()), user_name="Me"))

    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at is None     # timer cleared
    verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_staff_reply_legacy_clear_when_verify_disabled(initialized_db, monkeypatch):
    """With HDE verify disabled, fall back to the timestamp comparison: a
    staff_reply newer than the client clears the timer and makes no API call."""
    notify = to_storage(utcnow() + timedelta(minutes=10))
    lcr = to_storage(utcnow() - timedelta(minutes=1))
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=notify, last_client_reply_at=lcr,
    )
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", False)
    verify = AsyncMock(return_value=False)
    monkeypatch.setattr(topic_manager, "_hde_staff_replied_since", verify)
    bot = make_bot()

    await handle_staff_reply(bot, make_payload(last_post_date=to_storage(utcnow())))

    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at is None     # cleared (legacy timestamp path)
    verify.assert_not_awaited()              # no HDE API call
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:
```bash
python -m pytest tests/test_topic_manager.py -k "autoreply_does_not_clear or operator_reply_clears or legacy_clear_when_verify_disabled" -v
```
Expected: `test_staff_reply_autoreply_does_not_clear_pre_sla` FAILS — under current code the staff_reply is newer than the client, so `should_clear=True` wipes the timer (`assert rec.pre_sla_notify_at == notify` fails; `verify` is never awaited). The other two may pass incidentally — the autoreply test is the one that must go red.

- [ ] **Step 3: Implement the operator-verify gate**

In `bot/topic_manager.py`, replace the single `should_clear` line in `_handle_staff_reply_locked`:

```python
    reply_at_sec = reply_at.replace(microsecond=0)
    last_client_sec = (
        last_client.replace(microsecond=0) if last_client else None
    )
    should_clear = last_client_sec is None or reply_at_sec > last_client_sec
```

with:

```python
    reply_at_sec = reply_at.replace(microsecond=0)
    last_client_sec = (
        last_client.replace(microsecond=0) if last_client else None
    )
    newer_than_client = last_client_sec is None or reply_at_sec > last_client_sec
    if not newer_than_client:
        # stale / out-of-order / at-or-before the client message — never clears
        should_clear = False
    elif config.presla_hde_verify and config.hde_owner_id.strip():
        # Only the operator's own HDE post should clear the timer. Automated
        # replies (PosifloraSupportBot, dispatcher echoes) fire staff_reply too
        # but have a different HDE user_id — verify the operator genuinely
        # replied after the client before wiping the pre-SLA timer.
        should_clear = await _hde_staff_replied_since(
            ticket_id, record.last_client_reply_at
        )
    else:
        # No HDE verification available — trust the staff_reply event (legacy).
        should_clear = True
```

The `PRESLA-DIAG staff_reply ... should_clear=...` log line immediately below and all DB-update logic stay unchanged.

- [ ] **Step 4: Run the new tests to verify they pass**

Run:
```bash
python -m pytest tests/test_topic_manager.py -k "autoreply_does_not_clear or operator_reply_clears or legacy_clear_when_verify_disabled" -v
```
Expected: 3 passed.

- [ ] **Step 5: Run the full topic-manager suite (no regressions)**

Run:
```bash
python -m pytest tests/test_topic_manager.py -v
```
Expected: all pass — including the existing `test_staff_reply_clears_pre_sla` (empty `hde_owner_id` → legacy `else` branch → still clears), `test_staff_reply_keeps_pre_sla_when_client_reply_is_newer` (older staff reply → `newer_than_client=False`), and `test_staff_reply_same_second_does_not_clear_pre_sla` (same second → not newer → no clear, no API call). If a pre-existing unrelated failure shows up (see memory: scheduler SLA alert test has had flakiness), confirm it fails on `main` too before treating it as a regression.

- [ ] **Step 6: Commit**

```bash
git add bot/topic_manager.py tests/test_topic_manager.py \
        docs/superpowers/specs/2026-06-03-presla-ignore-autoreply-design.md \
        docs/superpowers/plans/2026-06-03-presla-ignore-autoreply.md
git commit -m "fix(presla): clear timer only on verified operator reply, not auto-replies"
```

---

## Deploy (after merge to main)

Per the project pipeline: push to `main`, then SSH to config1 → `git pull` in `/opt/hde-bot` → `systemctl restart hde-bot`. Confirm `systemctl status hde-bot` is active, then watch for `PRESLA-DIAG staff_reply ... should_clear=False` on auto-replies and an eventual genuine pre-SLA send in the journal.

## Self-Review

- **Spec coverage:** behavior table (4 rows) → `not newer_than_client` (row 3), `elif` verify True/False (rows 1–2), `else` legacy (row 4). Fail-open → `_hde_staff_replied_since` returns False on error (unchanged helper). All three TDD cases map to spec §Testing. ✓
- **Placeholders:** none — full test + impl code inline. ✓
- **Type/name consistency:** `_hde_staff_replied_since(ticket_id, since_storage)` signature matches the call; `config.presla_hde_verify` (bool) and `config.hde_owner_id` (str) match config.py. ✓
