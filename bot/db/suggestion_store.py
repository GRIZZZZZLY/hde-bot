"""Store + pure helpers for ai_suggestions and ai_suggestion_events (Phase 0A tracing)."""
from __future__ import annotations

import hashlib

import aiosqlite

from .core import connect


def compute_idempotency_key(
    ticket_id: str,
    trigger_source: str,
    context_until_post_id: str | None,
    pipeline_version: str | None,
    prompt_version: str | None,
) -> str:
    """Deterministic key. Includes prompt_version so a prompt change is captured
    even when pipeline_version is not bumped (refinement 1)."""
    raw = "|".join(
        [
            str(ticket_id),
            str(trigger_source),
            str(context_until_post_id or ""),
            str(pipeline_version or ""),
            str(prompt_version or ""),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def derive_human_label(event_types: list[str]) -> str | None:
    """Итог по полной цепочке событий, не по последнему (refinement 2).

    sent → accepted (или corrected, если была правка); rejected → rejected;
    approved → accepted; edited без отправки → corrected; иначе None.
    """
    s = set(event_types)
    if "sent" in s:
        return "corrected" if "edited" in s else "accepted"
    if "rejected" in s:
        return "rejected"
    if "approved" in s:
        return "accepted"
    if "edited" in s:
        return "corrected"
    return None


async def record_suggestion(
    *,
    ticket_id: str,
    topic_id: int | None,
    trigger_source: str,
    context_until_post_id: str | None,
    pipeline_version: str | None,
    prompt_version: str | None,
    title: str = "",
    history: str = "",
    client_text: str = "",
    ai_answer: str = "",
    ai_full_text: str = "",
    client_id: str | None = None,
    model: str | None = None,
) -> int:
    """Insert a suggestion row (idempotent on idempotency_key). Returns its id."""
    key = compute_idempotency_key(
        ticket_id, trigger_source, context_until_post_id, pipeline_version, prompt_version
    )
    async with connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_suggestions "
            "(ticket_id, topic_id, trigger_source, context_until_post_id, client_id, "
            " idempotency_key, title, history, client_text, ai_answer, ai_full_text, "
            " pipeline_version, prompt_version, model) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_id, topic_id, trigger_source, context_until_post_id, client_id,
                key, title, history, client_text, ai_answer, ai_full_text,
                pipeline_version, prompt_version, model,
            ),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM ai_suggestions WHERE idempotency_key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def get_suggestion(suggestion_id: int) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ai_suggestions WHERE id=?", (suggestion_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_open_suggestion_by_topic(topic_id: int) -> dict | None:
    """Most recent suggestion for a topic — the one the feedback buttons act on
    (ai_feedback_pending is single-per-topic, so latest == the pending one)."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ai_suggestions WHERE topic_id=? ORDER BY id DESC LIMIT 1",
            (topic_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None
