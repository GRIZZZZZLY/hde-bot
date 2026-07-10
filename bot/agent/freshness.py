"""Freshness guard: before publishing a suggestion, verify the ticket has not
advanced since the anchor post the suggestion was built on (spec Phase 0)."""
from __future__ import annotations


async def check_freshness(
    ticket_id: str,
    context_until_post_id: str | None,
    *,
    client=None,
) -> str:
    """Return 'current' if the newest HDE post equals the anchor, else 'superseded'.

    No posts (or unreadable ids) → 'current' (nothing to supersede)."""
    if client is None:
        from ..hde_api import HDEApiClient
        client = HDEApiClient()
    posts = await client.get_ticket_posts(ticket_id)
    if not posts:
        return "current"
    try:
        latest = max(int(p.post_id) for p in posts)
    except (TypeError, ValueError):
        return "current"
    if context_until_post_id is None:
        return "superseded"
    try:
        anchor = int(context_until_post_id)
    except (TypeError, ValueError):
        return "current"
    return "current" if latest <= anchor else "superseded"
