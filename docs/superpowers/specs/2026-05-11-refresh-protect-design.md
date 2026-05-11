# Design: Refresh Protection + Formatter Cleanup

**Date:** 2026-05-11  
**Status:** Approved

## Problem

1. `/refresh` blindly deletes Telegram topics for tickets not found in `get_my_open_tickets`. If the ticket is still open in HDE (e.g., API lag, reassignment race), the topic is permanently lost.
2. Deleted topics in the refresh summary show `{company_name}` literally (old DB rows stored unresolved template string) instead of a useful link.

## Goals

- Never delete a Telegram topic unless HDE confirms the ticket is closed/resolved.
- Show ticket name + "Открыть в HDE" link in the "Удалено" section. No company field.

## Non-Goals

- Protecting `pending_delete` cleanup (Step 3/4) — those are already bot-controlled state.
- Fixing `{company_name}` in the DB rows — formatter fix is sufficient.

## Design

### 1. `hde_api.py` — new method

```python
async def get_ticket_open_status(self, ticket_id: str) -> tuple[bool, str] | None:
```

- Calls `GET /tickets/{ticket_id}/`
- Returns `(is_deletable: bool, link_staff: str)` where `is_deletable = status in {"resolved", "closed"}`
- Returns `None` on any API error (fail-safe: caller skips deletion)
- Reuses existing `_read_response` / `_extract_error_message` helpers

### 2. `refresh.py` — Step 2 protection

Before deleting each stale topic:

```
result = await client.get_ticket_open_status(topic.ticket_id)
if result is None:          # API error → skip (fail-safe)
    continue
is_deletable, link = result
if not is_deletable:        # open/pending/unknown → skip
    continue
# resolved/closed → delete and record (ticket_name, ticket_id, link)
```

`RefreshResult.deleted` type changes from `list[db.TicketTopic]` to `list[tuple[str, str, str]]`  
= `list[(ticket_name, ticket_id, link_staff)]`

### 3. `formatter.py` — deleted section

Input changes from `list[db.TicketTopic]` to `list[tuple[str, str, str]]`.

Output:
```
🗑️ Удалено устаревших: <b>N</b>
  • Фискализация бьется безналом · <a href="https://...">Открыть в HDE</a>
```

No company field. `_escape` applied to ticket name. Link used as-is (comes from HDE API).

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| `get_ticket_open_status` → None (network error) | Skip deletion (fail-safe) |
| `get_ticket_open_status` → `(False, link)` (open/pending) | Skip deletion |
| `get_ticket_open_status` → `(True, link)` (resolved/closed) | Delete topic, include in report |
| Telegram delete fails | Log warning, continue (unchanged from current) |

## Files Changed

| File | Change |
|------|--------|
| `bot/hde_api.py` | Add `get_ticket_open_status` method |
| `bot/refresh.py` | Step 2: verify before delete; update `RefreshResult.deleted` type |
| `bot/formatter.py` | Update `format_refresh_result` deleted section |

## Testing

- `get_ticket_open_status`: mock HTTP 200 with `status=open`, `status=closed`, HTTP 404 → check return values
- `refresh_topics` Step 2: mock `get_ticket_open_status` returning open → topic not deleted; closed → deleted; None → not deleted
- `format_refresh_result`: pass `deleted=[(name, id, link)]`, assert company absent, link present
