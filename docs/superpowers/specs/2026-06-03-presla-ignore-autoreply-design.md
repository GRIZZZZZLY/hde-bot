# Pre-SLA: ignore automated staff replies when clearing the timer

**Date:** 2026-06-03
**Status:** Approved (design)
**Component:** `bot/topic_manager.py` — `_handle_staff_reply_locked`

## Problem

Pre-SLA notifications almost never fire. The pre-SLA timer is armed on the first
unanswered client reply (deadline = +10 min), but it is wiped before it can fire.

Observed on ticket 171623 (2026-06-03):

| Time (MSK) | Event |
|---|---|
| 09:50:48 | client reply → pre-SLA armed, deadline 10:00:48 |
| 09:54:01 | client reply (timer kept) |
| 09:54:02 | **staff_reply (+1 s) → timer cleared** |
| 10:07:59 | client reply → re-armed, deadline 10:17:59 |
| 10:08:14 | staff_reply → cleared again |
| 10:08:17 | ticket closed |

The `staff_reply` arriving ~1 second after the client is **not** the human
operator — it is an automated reply (PosifloraSupportBot reassurance and/or the
HDE dispatcher "TG bot | Ответ сотрудника" echo). It clears the timer, so the
operator-pacing safety net never triggers.

## Root cause

`_handle_staff_reply_locked` ([topic_manager.py:1161](../../bot/topic_manager.py))
clears the timer on **any** `staff_reply` event whose timestamp is newer than the
last client reply:

```python
should_clear = last_client_sec is None or reply_at_sec > last_client_sec
```

It does not check **who** posted. Automated replies (different HDE `user_id`)
satisfy the timestamp condition and wipe the timer.

The scheduler's self-heal already solves the "who" problem correctly:
`_hde_staff_replied_since` ([topic_manager.py:1304](../../bot/topic_manager.py))
treats a reply as genuine only when `post.user_id == config.hde_owner_id` (the
operator). Automated replies have a different `user_id` and are ignored.

**Confirmed precondition:** the operator (Игорь Кравцов) replies *directly in
HDE* under their own account, so genuine replies are authored by `hde_owner_id`.
The bot does not post operator replies on their behalf.

## Approach (B): live-verify in the webhook

Reuse `_hde_staff_replied_since` in the `staff_reply` webhook path. Clear the
timer only when the **operator** actually posted after the last client reply.
Automated replies fail the check and leave the timer armed.

Rejected alternatives:
- **A — author field from payload:** the webhook payload's `owner_id` is the
  ticket's assigned owner, not the post author; no reliable author field is
  known without inspecting raw payloads. Fragile.
- **C — match auto-reply text:** brittle; multiple auto-reply templates exist.

## Design

Replace the `should_clear` computation in `_handle_staff_reply_locked`:

```python
newer_than_client = last_client_sec is None or reply_at_sec > last_client_sec

if not newer_than_client:
    # stale / out-of-order / echo at-or-before the client message — never clears
    should_clear = False
elif config.presla_hde_verify and config.hde_owner_id.strip():
    # Verify the OPERATOR genuinely replied after the client. Automated replies
    # (PosifloraSupportBot, dispatcher echoes) have a different HDE user_id and
    # must not wipe the timer.
    should_clear = await _hde_staff_replied_since(
        ticket_id, record.last_client_reply_at
    )
else:
    # No HDE verification available — trust the event (legacy behavior).
    should_clear = True
```

Everything after `should_clear` (pre-SLA message deletion, DB update,
`last_staff_reply_at` stamping, implicit-feedback) is unchanged. The diagnostic
`PRESLA-DIAG staff_reply ... should_clear=...` log line is retained.

### Behavior

| Scenario | Operator post after client? | should_clear | Result |
|---|---|---|---|
| Operator replies in HDE | yes | True | timer cleared (correct) |
| Auto-reply only (PosifloraSupportBot / echo) | no | False | timer survives → pre-SLA can fire |
| staff_reply at/before last client (echo) | n/a | False | timer survives (existing guard) |
| `presla_hde_verify` disabled | — | timestamp legacy | unchanged from today |

### Fail-open

`_hde_staff_replied_since` returns `False` on any HDE API error. Here that means
the timer is **not** cleared on error → biases toward sending the pre-SLA alert
rather than silently swallowing it. Consistent with the feature's intent.

## Edge cases

- **Visibility lag:** assumes the operator's HDE post is queryable when the
  webhook fires. The dispatcher emits the webhook after the post is committed;
  the scheduler already relies on this. Accepted.
- **>5 posts since client:** `get_ticket_posts(limit=5)` may miss the operator
  post in a very chatty window. Matches existing scheduler behavior; accepted.
- **Operator replied to an earlier client message:** `since =
  last_client_reply_at` (the newest client message), so an older operator post
  is correctly not counted — the new client message is still unanswered.

## Testing (TDD)

New tests in the existing staff-reply test module, mocking the HDE posts call by
the established pattern:

1. **Auto-reply does not clear** — staff_reply newer than client, but
   `_hde_staff_replied_since` → False ⇒ `pre_sla_notify_at` preserved.
2. **Operator reply clears** — `_hde_staff_replied_since` → True ⇒ timer cleared
   (`pre_sla_notify_at` None).
3. **Verify disabled → legacy** — `presla_hde_verify=False` ⇒ falls back to
   timestamp comparison (clears when newer).

Existing `test_staff_reply_same_second_does_not_clear_pre_sla` must stay green
(same-second still not newer → no clear, no API call).

## Scope

~10 lines in one function + 3 tests. No new modules, no schema change, no new
config. Deploy via existing pipeline (push to main → SSH pull + restart on
config1).
