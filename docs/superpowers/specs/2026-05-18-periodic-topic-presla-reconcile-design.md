# Periodic Personal-Topic + Pre-SLA Reconcile — Design

Date: 2026-05-18
Status: Approved (pending written-spec review)

## Problem

A ticket assigned to the operator (via HDE UI or the Telegram button) relies on a
webhook (`assigned_on_create` / `owner_changed`) to create its forum topic. If that
webhook is lost or fails, the topic is never created. The operator may not notice.
Worse: the pre-SLA timer is armed **only** in `handle_client_reply`
(`topic_manager._schedule_pre_sla`, single call site at `topic_manager.py:947`).
A missed `client_reply` webhook — or a client message that arrived before the topic
existed — means `pre_sla_notify_at` stays `None`, so the scheduler never fires the
pre-SLA alert and never sends the auto-reassurance. The SLA burns silently.

The manual `/refresh` command (`refresh.refresh_topics`) already reconciles topics,
but it is operator-triggered, not periodic, and it does not arm pre-SLA timers.

## Goal

A periodic, automatic safety net that:

1. Creates missing topics for open tickets currently assigned to the operator.
2. Arms the pre-SLA timer for such tickets whose last HDE post is from the client
   (i.e. an unanswered message) and which have no timer yet.
3. Alerts the operator in Telegram when it had to recover anything, signalling that
   the webhook channel is unreliable.

Out of scope: changing the webhook path, changing `/refresh` (Steps 0–3 unchanged),
changing pre-SLA dispatch/guard logic.

## Architecture

New async function `reconcile_personal_topics(bot)` in `bot/refresh.py`. It reuses
`HDEApiClient`, `topic_manager.sync_ticket_topic`, and
`topic_manager._schedule_pre_sla`. It is invoked from the existing scheduler tick in
`bot/scheduler.py`, immediately after `_maybe_reconcile_general(bot)`, behind a
throttle of ~10 minutes modelled on the existing
`_last_general_reconcile_at` / `_GENERAL_RECONCILE_INTERVAL_SEC` pattern
(new module-level `_last_personal_reconcile_at` + interval constant).

The function is a pure additive overlay: one new function, one call site in the
scheduler tick, one throttle variable. No webhook-path or `/refresh` changes.

## Data Flow (one cycle)

1. `client.get_my_open_tickets()` → open/process tickets with `owner_id` = the
   operator (`config.hde_owner_id`, 98). On `HDEApiError` → log warning, abort the
   cycle, do **not** update the throttle timestamp (retry next tick). Fail-safe,
   mirroring `/refresh` Step 2.
2. For each ticket, load the DB record (`db.get_topic`).
   - Skip entirely if the record exists and is `is_pending_delete` or `is_deleted`
     (a ticket in the 10-minute removal window must not be resurrected).
   - If no active topic exists (record `None` or deleted-state mismatch handled by
     `sync_ticket_topic`'s own `_ensure_active_topic`) → `sync_ticket_topic(bot,
     payload)` creates the topic. Idempotent when the topic already exists.
3. Pre-SLA arming, only when `record.pre_sla_notify_at is None` **and**
   `record.pre_sla_sent_at is None` (do not revive a completed cycle, do not
   double-arm):
   - `client.get_ticket_posts(ticket_id, limit=5)`; drop internal comments
     (`is_comment`). Take the most recent remaining post.
   - Staff vs client: a post is **staff** when `post.user_id == int(config.hde_owner_id)`
     (same rule as `topic_manager._hde_staff_replied_since`). Otherwise **client**.
   - Last post is client → `_schedule_pre_sla(ticket_id, payload,
     last_client_reply_at=<storage form of post.date_created>)`. The payload carries
     the ticket's `sla_date` so `_calculate_pre_sla_notify_at` computes the deadline;
     `last_client_reply_at` must be the real client-post time so the existing
     `_hde_staff_replied_since` dispatch guard stays correct.
   - Last post is staff, or no posts, or posts fetch fails → do not arm
     (operator already answered / cannot confirm). Log at debug.
4. Track `recovered`: count of tickets for which this cycle created a topic or
   armed a timer that was missing due to a lost webhook.

### Post date conversion

`HDEPost.date_created` is `"HH:MM:SS DD.MM.YYYY"` (not the format `parse_datetime`
accepts — see prior finding). The implementation must convert it to storage form
using the existing HDE-date helper used elsewhere for HDE post dates; the exact
helper is resolved during planning, not assumed here.

## Alert

If `recovered > 0`, send one personal Telegram message to the operator destination
(operator chat / general topic, consistent with how other operator-facing
notifications choose their destination):

> ⚠️ Восстановлено N тикетов после пропущенного webhook: #id, #id, …

`recovered == 0` → no message (silent, no noise).

## Error Handling

- `get_my_open_tickets` raises → log warning, abort cycle, throttle timestamp not
  updated (retry next tick).
- `get_ticket_posts` raises for a single ticket → skip pre-SLA arming for that
  ticket only, continue with the rest; do not abort the cycle.
- Telegram flood on topic creation → low risk (only missed-webhook tickets, rare,
  one `sync_ticket_topic` per ticket, no batch edits).
- Throttle timestamp is updated only on a successful cycle entry, never on the
  error path.

## Edge Cases

1. Ticket assigned but topic already active → `sync_ticket_topic` idempotent
   (`_ensure_active_topic` else-branch); pre-SLA untouched if already armed.
2. Ticket in `pending_delete` (recently unassigned, 10-min window) → skip topic
   and timer (guarded by step 2 skip).
3. Ticket `closed` → not returned by `get_my_open_tickets` (status open/process) →
   never processed.
4. Last post is the bot's own reassurance / system post → expected to carry the
   operator's `user_id` (reassurance is sent via `HDEApiClient.add_post` on the
   configured operator account) → treated as staff → not armed → no auto-reply
   loop. Planning must verify `add_post` actually authors as `hde_owner_id`; if
   not, add an explicit reassurance-author exclusion.
5. `get_ticket_posts` empty → not armed, debug log.
6. `pre_sla_notify_at` set or `pre_sla_sent_at` set → not re-armed.
7. Telegram flood on mass create → accepted low risk (rare path).

## Testing (TDD, pytest + AsyncMock, matching existing scheduler tests)

- `test_reconcile_creates_missing_topic` — open operator ticket, no DB record →
  topic created, `recovered == 1`.
- `test_reconcile_skips_existing_active_topic` — topic exists → no recreate,
  `recovered == 0`.
- `test_reconcile_arms_presla_when_last_post_client` — no timer, last post client
  → `_schedule_pre_sla` called with the client post date.
- `test_reconcile_skips_presla_when_last_post_staff` — last post staff →
  `_schedule_pre_sla` not called.
- `test_reconcile_skips_presla_when_already_armed` — `pre_sla_notify_at` set →
  untouched.
- `test_reconcile_skips_presla_when_already_sent` — `pre_sla_sent_at` set →
  untouched.
- `test_reconcile_skips_pending_delete` — `pending_delete` record → neither topic
  nor timer.
- `test_reconcile_throttled` — second call inside the 10-minute window → no-op.
- `test_reconcile_hde_error_failsafe` — `get_my_open_tickets` raises → warning
  logged, throttle not advanced, no side effects.
- `test_reconcile_posts_error_skips_one_ticket` — `get_ticket_posts` raises for
  one ticket → that ticket's timer skipped, others still processed.
- `test_reconcile_alert_sent_when_recovered` — `recovered > 0` → personal Telegram
  message sent.
- `test_reconcile_no_alert_when_nothing_recovered` — `recovered == 0` → no message.

## Affected Files

- `bot/refresh.py` — new `reconcile_personal_topics(bot)`.
- `bot/scheduler.py` — new throttle var + interval constant + one call in the tick.
- Tests — new test module alongside existing scheduler/refresh tests.
