# HDE post author-type — findings (Phase 2A, Task 0)

**Status: unverified against live API — static inspection only.** Prod HDE
credentials are not exercised from this session, so the raw `/tickets/{id}/posts/`
JSON was not captured live. The finding below is derived from how the codebase
already constructs `HDEPost`, which is sufficient to fix the staff-detection rule
because `is_staff_post` operates on `HDEPost` objects, not raw JSON.

## What the code reads from a raw post

`HDEApiClient.get_ticket_posts` (`bot/hde_api.py:315-335`) builds each `HDEPost`
from exactly these raw keys:

- `id`        → `post_id: int`
- `user_id`   → `user_id: int`
- `text`      → `text` (raw HTML)
- `date_created` → `date_created` ("HH:MM:SS DD.MM.YYYY")
- `user_name` + `user_lastname` → `user_name` (display only)
- `files`     → attachments

`HDEPost` (`bot/hde_api.py:69-81`) carries **no** author-type / role / `user_type`
/ `is_staff` / system flag. `is_comment` distinguishes public posts (`/posts/`,
`False`) from internal comments (`/comments/`, `True`) — it is NOT an author role.

## Consequence for staff detection (Task 4)

There is no author-type field to prefer, so `is_staff_post(post, staff)` uses:

1. **Configured staff-id set** — `AGENT_STAFF_USER_IDS` (CSV) via
   `staff_id_set(owner_id, staff_ids)`.
2. **Owner fallback** — if `AGENT_STAFF_USER_IDS` is empty, the ticket owner id is
   treated as the sole staff id (`{owner_id}`).

Bot/system posts cannot be identified from the parsed `HDEPost`; they are only
excluded insofar as their `user_id` is not in the staff set (so they fall to the
"client" side). If a system/bot user_id is known, add it to a client-exclusion
later — out of scope for 2A. **Recommendation:** populate `AGENT_STAFF_USER_IDS`
with the real operator ids before running the backfill so co-worker replies are
attributed to the operator side rather than to the client.

## Pagination note (Task 3)

`get_ticket_posts` currently sends only `{"limit": str(limit)}` (no `page`).
`get_all_ticket_posts` adds an optional `page` param, threaded into the query the
same way the tickets-list endpoints (`get_my_open_tickets`,
`get_closed_tickets_page`) already use `page`. **Pagination of the `/posts/`
sub-endpoint is assumed by API analogy and is unverified live.** Because
`split_ticket_into_pairs` re-sorts posts explicitly by `(post_id, date_created)`,
cross-page ordering is safe even if pages arrive newest-first per page. If a live
check later shows the `/posts/` endpoint ignores `page`, fall back to a single
`get_ticket_posts(ticket_id, limit=200)` call.
