# Client History in Topic — Design Spec

**Goal:** When a new Telegram topic is created for a ticket, send a second message showing the client's past tickets from HDE.

**Architecture:** Three changes to existing files. No new modules.

---

## Data Flow

1. New ticket webhook fires → `sync_ticket_topic()` → `_create_topic()` → `_post_ticket_history()`
2. After history is posted, call `_post_client_history(bot, record, ticket_id)`
3. `_post_client_history` calls `get_ticket_info(ticket_id)` to get `client_id`
4. Calls `hde_api.get_client_tickets(client_id, limit=10)` — HDE `/tickets/?user_list={client_id}`
5. Filters out current ticket, formats and sends message to topic
6. If any step fails → log warning, skip silently (topic creation is unaffected)

---

## Changes

### `bot/hde_api.py`

New method on `HDEApi`:

```python
async def get_client_tickets(self, client_id: int, limit: int = 10) -> list[dict]:
    """Return up to `limit` tickets for the given client (requester), newest first."""
```

Query: `GET /tickets/?user_list={client_id}&order_by=id&order_dir=desc`

Returns raw ticket dicts with fields: `id`, `unique_id`, `subject`, `status`, `created_at`.

Pagination: fetch page 1 only (30 results default), slice to `limit`. No full pagination needed.

### `bot/formatter.py`

New function:

```python
def format_client_history(
    client_name: str,
    total: int,
    recent_titles: list[str],       # up to 5 ticket subjects
    last_ticket_date: str | None,   # human-readable relative date, e.g. "3 дня назад"
) -> str:
```

Output (HTML):
```
🏢 <b>Клиент: {client_name}</b> — {total} обращений

📋 Последние темы:
• {title1}
• {title2}
• {title3}

🕐 Последнее: {last_ticket_date}
```

If `total == 0` → return empty string (caller skips sending).

### `bot/topic_manager.py`

New private async function `_post_client_history(bot, topic_id, ticket_id)`.

Called at the end of `_post_ticket_history()` — after all ticket messages are sent.

```python
async def _post_client_history(bot: Bot, topic_id: int, ticket_id: str) -> None:
    try:
        info = await hde_api_instance.get_ticket_info(ticket_id)
        tickets = await hde_api_instance.get_client_tickets(info.client_id)
        # filter out current ticket
        past = [t for t in tickets if str(t["id"]) != ticket_id]
        if not past:
            return
        recent_titles = [t["subject"] for t in past[:5]]
        last_date = _relative_date(past[0].get("created_at"))
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

`_relative_date(iso_str)` — helper that converts HDE date string to «N дней назад» / «сегодня» / «вчера».

---

## Edge Cases

| Scenario | Behaviour |
|---|---|
| First ticket from this client | `past` is empty → no message sent |
| HDE API error | Logged as warning, topic creation continues |
| `client_id` unavailable | `get_ticket_info` returns None → skip |
| Current ticket in results | Filtered out by `ticket_id` comparison |
| More than 5 past tickets | Show only 5 most recent titles, display real total |

---

## What is NOT included

- No links to past tickets (keeps message short)
- No classification of ticket types (raw titles only)
- No caching of client history between sessions
