# General Channel — Unassigned Tickets Notifications

**Date:** 2026-04-06  
**Status:** Approved

## Context

The bot currently tracks tickets assigned to a specific operator (Igor Kravtsov) and creates individual Telegram forum topics for each. There's no visibility into tickets that arrive unassigned — specifically those in the "Оборудование" department with no assigned owner. The team needs a shared General topic in the Telegram group where these tickets appear automatically, so anyone can pick them up.

## Requirements

- When an unassigned ticket (owner = empty/"Неприсвоенный") arrives in the configured department, post a short notification in the General topic
- If the ticket's subject changes, edit the notification in place
- When the ticket gets assigned to someone (owner_changed event with a real owner), delete the notification
- When the ticket is closed, delete the notification
- Controlled by two env vars: `GENERAL_TOPIC_ID` and `UNASSIGNED_DEPARTMENT`

## Architecture

New isolated module `bot/general_channel.py` — contains all logic for General topic notifications. No changes to `topic_manager.py` or existing ticket handling logic.

New SQLite table `unassigned_general_messages` persists the `message_id` of each posted notification so it can be edited or deleted later.

Hooks added in `hde_webhook.py` — existing handlers run first, then `general_channel` handlers run on top for the same events.

## Files to Modify

- `bot/general_channel.py` — **new file**, all General topic logic
- `bot/db.py` — add `unassigned_general_messages` table + CRUD functions
- `bot/hde_webhook.py` — add `department` field to `_normalize_payload`; call general_channel handlers after existing ones
- `bot/config.py` — add `general_topic_id: int | None` and `unassigned_department: str` fields
- `.env.example` — document new env vars

## Data Model

```sql
CREATE TABLE IF NOT EXISTS unassigned_general_messages (
    ticket_id   TEXT PRIMARY KEY,
    message_id  INTEGER NOT NULL,
    ticket_name TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
)
```

## Filter Logic

A ticket is "unassigned in target department" when:
1. `owner_name` is empty OR `owner_name.lower()` contains `"неприсвоенный"`
2. AND either `UNASSIGNED_DEPARTMENT` is not set, OR `department` matches it (case-insensitive)

`department` is extracted from the raw HDE payload field `department` (added to `_normalize_payload`).

## Event Handling

| HDE Event | Condition | Action |
|---|---|---|
| `assigned_on_create` | ticket is unassigned + target dept | post notification, save message_id |
| `owner_changed` | ticket now has a real owner | delete notification if exists |
| `owner_changed` | ticket became unassigned again | post notification if not already exists |
| `ticket_updated` | ticket_name changed | edit existing notification |
| `ticket_closed` | notification exists | delete notification |

## Message Format

```
🆕 <b>Неприсвоенный тикет</b>

#А-12345 — Название тикета
<a href="https://...">Открыть в HDE</a>
```

On edit: same template with updated ticket_name. Sent with `parse_mode="HTML"`, `disable_web_page_preview=True`.

## Configuration

```env
GENERAL_TOPIC_ID=12345            # message_thread_id of General topic (required to enable)
UNASSIGNED_DEPARTMENT=Оборудование  # filter by department (optional — if unset, all unassigned tickets)
```

If `GENERAL_TOPIC_ID` is not set, the entire feature is silently disabled — no errors.

## Verification

1. Set `GENERAL_TOPIC_ID` and `UNASSIGNED_DEPARTMENT=Оборудование` in `.env`
2. Simulate `assigned_on_create` webhook with `owner_name=""`, `department="Оборудование"` → message appears in General topic
3. Simulate `ticket_updated` with new `ticket_name` → message is edited
4. Simulate `owner_changed` with a real owner → message is deleted
5. Simulate `ticket_closed` → message is deleted
6. Simulate `assigned_on_create` with `department="Другой"` → no message posted
