# Pre-SLA Safety Net — Design Spec

**Date:** 2026-05-11
**Status:** Approved

## Problem

Two failure modes:

1. **HDE webhook arrives late** (e.g. 31 min delay). Bot sees `last_post_date = 11:30`, computes
   `pre_sla_notify_at = 11:50`. Webhook lands at 12:01 → deadline already past → bot fires
   pre-SLA alert immediately, even though operator already replied at 11:35 via HDE UI.

2. **`staff_reply` webhook lost/delayed.** Bot never clears `pre_sla_notify_at`, so scheduler
   keeps sending countdown alerts every minute despite the operator having already responded.

## Solution

**A — HDE live-check before firing.** Before sending any pre-SLA alert, call HDE API to fetch the
last post. If the last post is from staff (not client) and is newer than `last_client_reply_at` →
the operator already replied. Clear pre-SLA locally (self-heal the missed webhook) and skip alert.

**B — Auto-reassurance at T-N minutes.** When pre-SLA has not been defused and N minutes remain,
automatically send a reassurance post to the client via HDE API and notify the topic. Only fires
once per ticket.

## Data Flow

### A — HDE verify guard

```
scheduler tick
  list_due_pre_sla() → records
  for each record:
    _hde_staff_replied_since(ticket_id, last_client_reply_at)
      → get_ticket_posts(ticket_id, limit=5)
      → check: any post with is_staff=True AND date > last_client_reply_at?
    if yes → db.clear_pre_sla(ticket_id)   # self-heal
              log.info("pre-SLA cleared: staff already replied in HDE")
              continue
    if no  → send_pre_sla_alert(bot, record)  # normal path

  list_active_pre_sla() → records (countdown updates)
  for each record:
    same _hde_staff_replied_since check
    if yes → _try_delete_pre_sla_message + clear_pre_sla; continue
    if no  → update_pre_sla_alert(bot, record)
```

### B — Auto-reassurance

```
scheduler tick (same process_scheduled_actions)
  for record in active tickets with pre_sla_notify_at set:
    minutes_left = _pre_sla_minutes_left(record)
    if minutes_left <= REASSURANCE_MINUTES_BEFORE
    AND record.reassurance_sent_at IS NULL:
      _hde_staff_replied_since check → if staff replied: clear & skip
      hde_api.add_post(ticket_id, REASSURANCE_TEXT)
      db.update_topic(ticket_id, reassurance_sent_at=now())
      bot.send_message(topic, "🤖 Автоответ клиенту отправлен")
```

## HDE Post Author Detection

`get_ticket_posts` returns `HDEPost` objects. Need `is_staff` flag or user-type field.
Check `HDEPost` dataclass for `author_type` or compare `owner_id`. If no staff flag available,
fallback: last post `owner_id` == `config.hde_owner_id` → staff.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `REASSURANCE_MINUTES_BEFORE` | `2` | Minutes before SLA deadline to auto-send |
| `REASSURANCE_TEXT` | `"Я про вас не забыл, занимаюсь вашим вопросом 🔧"` | Text sent to client via HDE |
| `PRESLA_HDE_VERIFY` | `1` | Set `0` to disable live HDE check (emergency off) |

## Files

| File | Change |
|---|---|
| `bot/hde_api.py` | Verify `HDEPost` has enough fields; add `is_staff` if needed |
| `bot/topic_manager.py` | Add `_hde_staff_replied_since()`, `send_reassurance_to_client()` |
| `bot/scheduler.py` | Guard both pre-SLA loops; add reassurance loop |
| `bot/db.py` | Add `reassurance_sent_at` column; clear in `clear_pre_sla` |
| `bot/config.py` | Add `reassurance_minutes_before`, `reassurance_text`, `presla_hde_verify` |
| `.env.example` | Document new vars |
| `tests/test_presla_safety.py` | New test file |

## Error Handling

- HDE API call in guard fails → log warning, proceed with sending alert (fail-open: better a
  false-positive alert than a missed one).
- `add_post` for reassurance fails → log warning, do NOT set `reassurance_sent_at` (retry next tick).
