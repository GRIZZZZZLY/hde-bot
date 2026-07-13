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
    action_type: str | None = None,
    self_check: str | None = None,
    retrieved_refs: str | None = None,
    confidence: int | None = None,
    confidence_reason: str | None = None,
    retrieval_query: str | None = None,
    retrieval_config_version: str | None = None,
    embedding_model: str | None = None,
    generation_ms: int | None = None,
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
            " pipeline_version, prompt_version, model, action_type, self_check, "
            " retrieved_refs, confidence, confidence_reason, retrieval_query, "
            " retrieval_config_version, embedding_model, generation_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_id, topic_id, trigger_source, context_until_post_id, client_id,
                key, title, history, client_text, ai_answer, ai_full_text,
                pipeline_version, prompt_version, model, action_type, self_check,
                retrieved_refs, confidence, confidence_reason, retrieval_query,
                retrieval_config_version, embedding_model, generation_ms,
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


async def get_unjudged_suggestions(hours: int = 24, limit: int = 200) -> list[dict]:
    """Свежие предложения без вердикта — кандидаты на ночную сверку с фактическим
    ответом оператора. Только с непустым ai_answer и известным якорем."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ai_suggestions "
            "WHERE judged_at IS NULL AND ai_answer IS NOT NULL AND ai_answer != '' "
            "AND context_until_post_id IS NOT NULL "
            "AND created_at >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT ?",
            (f"-{int(hours)} hours", int(limit)),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_judge_result(
    suggestion_id: int, *, reference_answer: str, label: str, detail: str = ""
) -> None:
    """Вердикт сверки: эталон = фактический ответ оператора; label канонический
    (accepted/corrected). Пересчитывает effective_label."""
    async with connect() as db:
        await db.execute(
            "UPDATE ai_suggestions SET judge_reference_answer=?, judge_label=?, "
            "judge_detail=?, judged_at=datetime('now') WHERE id=?",
            (reference_answer, label, detail, suggestion_id),
        )
        await db.commit()
    await _recompute_labels(suggestion_id)


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


_REVIEW_BY_EVENT = {"approved": "approved", "rejected": "rejected", "edited": "edited"}
_DELIVERY_BY_EVENT = {"send_requested": "requested", "sent": "sent", "send_failed": "failed"}


async def record_suggestion_event(
    suggestion_id: int,
    event_type: str,
    *,
    payload: str | None = None,
    hde_post_id: str | None = None,
) -> int:
    """Append an operator-action event and update denormalized status + labels."""
    async with connect() as db:
        cursor = await db.execute(
            "INSERT INTO ai_suggestion_events "
            "(suggestion_id, event_type, payload, hde_post_id) VALUES (?,?,?,?)",
            (suggestion_id, event_type, payload, hde_post_id),
        )
        if event_type in _REVIEW_BY_EVENT:
            await db.execute(
                "UPDATE ai_suggestions SET review_status=?, reviewed_at=datetime('now') WHERE id=?",
                (_REVIEW_BY_EVENT[event_type], suggestion_id),
            )
        if event_type in _DELIVERY_BY_EVENT:
            await db.execute(
                "UPDATE ai_suggestions SET delivery_status=? WHERE id=?",
                (_DELIVERY_BY_EVENT[event_type], suggestion_id),
            )
        if event_type == "sent":
            await db.execute(
                "UPDATE ai_suggestions "
                "SET final_sent_text=?, final_sent_post_id=?, sent_at=datetime('now') WHERE id=?",
                (payload or "", hde_post_id, suggestion_id),
            )
        await db.commit()
        event_id = int(cursor.lastrowid)
    await _recompute_labels(suggestion_id)
    return event_id


async def _recompute_labels(suggestion_id: int) -> None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT event_type FROM ai_suggestion_events WHERE suggestion_id=? ORDER BY id",
            (suggestion_id,),
        ) as cur:
            events = [r["event_type"] for r in await cur.fetchall()]
        async with db.execute(
            "SELECT judge_label FROM ai_suggestions WHERE id=?", (suggestion_id,)
        ) as cur:
            row = await cur.fetchone()
        judge_label = row["judge_label"] if row else None
        human = derive_human_label(events)
        effective = human if human is not None else judge_label
        await db.execute(
            "UPDATE ai_suggestions SET human_label=?, effective_label=? WHERE id=?",
            (human, effective, suggestion_id),
        )
        await db.commit()


async def collect_suggestion_daily_stats(hours: int = 24) -> dict:
    """Счётчики действий оператора по подсказкам за последние N часов —
    сырьё для ежедневного отчёта пользы (ревизия 3 roadmap)."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN delivery_status='sent' AND review_status!='edited'
                  THEN 1 ELSE 0 END) AS sent_no_edit,
              SUM(CASE WHEN delivery_status='sent' AND review_status='edited'
                  THEN 1 ELSE 0 END) AS sent_edited,
              SUM(CASE WHEN review_status='edited' THEN 1 ELSE 0 END) AS edited,
              SUM(CASE WHEN review_status='rejected' THEN 1 ELSE 0 END) AS rejected,
              SUM(CASE WHEN review_status='approved' THEN 1 ELSE 0 END) AS approved,
              SUM(CASE WHEN review_status='pending' AND delivery_status='not_sent'
                  THEN 1 ELSE 0 END) AS unused,
              AVG(CASE WHEN sent_at IS NOT NULL
                  THEN (julianday(sent_at) - julianday(created_at)) * 1440.0
                  END) AS avg_minutes_to_send
            FROM ai_suggestions
            WHERE created_at >= datetime('now', ?)
            """,
            (f"-{int(hours)} hours",),
        ) as cur:
            row = await cur.fetchone()
    stats = {k: row[k] for k in row.keys()} if row else {}
    for key, value in list(stats.items()):
        if value is None and key != "avg_minutes_to_send":
            stats[key] = 0
    return stats


async def get_reconciliation_digest(hours: int = 24, top: int = 5) -> dict:
    """Сводка ночной сверки за N часов: сколько предложений совпало с оператором
    (accepted) и разошлось (corrected) + топ расхождений (бот ↔ оператор) для ревью."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT judge_label, COUNT(*) AS n FROM ai_suggestions "
            "WHERE judged_at >= datetime('now', ?) AND judge_label IS NOT NULL "
            "GROUP BY judge_label",
            (f"-{int(hours)} hours",),
        ) as cur:
            counts = {r["judge_label"]: r["n"] for r in await cur.fetchall()}
        async with db.execute(
            "SELECT ticket_id, ai_answer, judge_reference_answer FROM ai_suggestions "
            "WHERE judged_at >= datetime('now', ?) AND judge_label='corrected' "
            "ORDER BY id DESC LIMIT ?",
            (f"-{int(hours)} hours", int(top)),
        ) as cur:
            diverged = [dict(r) for r in await cur.fetchall()]
    return {
        "matched": counts.get("accepted", 0),
        "diverged": counts.get("corrected", 0),
        "top_diverged": diverged,
    }
