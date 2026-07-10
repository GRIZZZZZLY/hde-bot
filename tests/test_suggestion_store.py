import bot.db as db_module
from bot.db.suggestion_store import (
    compute_idempotency_key,
    derive_human_label,
    get_open_suggestion_by_topic,
    get_suggestion,
    record_suggestion,
)


async def _columns(table: str) -> set[str]:
    from bot.db.core import connect
    async with connect() as db:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
    return {r[1] for r in rows}


async def test_ai_suggestions_table_created():
    await db_module.init_db()
    cols = await _columns("ai_suggestions")
    expected = {
        "id", "ticket_id", "topic_id", "trigger_source", "context_until_post_id",
        "client_id", "idempotency_key", "title", "history", "client_text",
        "retrieval_query", "retrieval_config_version", "embedding_model",
        "retrieved_refs", "pipeline_version", "prompt_version", "model",
        "action_type", "ai_answer", "ai_full_text", "confidence",
        "confidence_reason", "self_check", "generation_status", "review_status",
        "delivery_status", "evaluation_status", "freshness_status",
        "final_sent_text", "final_sent_post_id", "reviewed_at", "sent_at",
        "human_label", "judge_label", "judge_detail", "judge_reference_answer",
        "judged_at", "effective_label", "generation_ms", "tokens_in",
        "tokens_out", "error", "created_at",
    }
    assert expected <= cols


async def test_ai_suggestion_events_table_created():
    await db_module.init_db()
    cols = await _columns("ai_suggestion_events")
    assert {"id", "suggestion_id", "event_type", "payload", "hde_post_id", "created_at"} <= cols


def test_idempotency_key_stable_and_includes_prompt_version():
    a = compute_idempotency_key("T1", "first", "99", "v0", "legacy")
    b = compute_idempotency_key("T1", "first", "99", "v0", "legacy")
    assert a == b
    # refinement 1: prompt_version change alone yields a different key
    c = compute_idempotency_key("T1", "first", "99", "v0", "legacy-2")
    assert a != c


def test_derive_human_label_full_chain():
    assert derive_human_label(["approved"]) == "accepted"
    assert derive_human_label(["send_requested", "sent"]) == "accepted"
    assert derive_human_label(["edit_started", "edited", "send_requested", "sent"]) == "corrected"
    assert derive_human_label(["edit_started", "edited"]) == "corrected"
    assert derive_human_label(["rejected"]) == "rejected"
    assert derive_human_label(["send_requested", "send_failed"]) is None
    assert derive_human_label([]) is None


def test_derive_human_label_sent_dominates_last_event():
    # full-chain, not last-event: approved then sent stays accepted
    assert derive_human_label(["approved", "send_requested", "sent"]) == "accepted"


async def test_record_and_read_suggestion():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="T1", topic_id=555, trigger_source="first",
        context_until_post_id="99", pipeline_version="v0", prompt_version="legacy",
        title="Тема", history="диалог", ai_full_text="Клиенту: ...",
    )
    row = await get_suggestion(sid)
    assert row["ticket_id"] == "T1"
    assert row["topic_id"] == 555
    assert row["review_status"] == "pending"
    assert row["delivery_status"] == "not_sent"
    assert row["evaluation_status"] == "pending"
    assert row["freshness_status"] == "current"


async def test_record_suggestion_idempotent():
    await db_module.init_db()
    kw = dict(
        ticket_id="T2", topic_id=1, trigger_source="first",
        context_until_post_id="10", pipeline_version="v0", prompt_version="legacy",
    )
    first = await record_suggestion(**kw)
    again = await record_suggestion(**kw)
    assert first == again  # same key → same row, no duplicate


async def test_get_open_suggestion_returns_latest():
    await db_module.init_db()
    await record_suggestion(
        ticket_id="T3", topic_id=7, trigger_source="first",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
    )
    second = await record_suggestion(
        ticket_id="T3", topic_id=7, trigger_source="button",
        context_until_post_id="2", pipeline_version="v0", prompt_version="legacy",
    )
    row = await get_open_suggestion_by_topic(7)
    assert row["id"] == second


from bot.db.suggestion_store import record_suggestion_event


async def _new_suggestion(topic_id=1, ctx="1") -> int:
    return await record_suggestion(
        ticket_id="T", topic_id=topic_id, trigger_source="first",
        context_until_post_id=ctx, pipeline_version="v0", prompt_version="legacy",
    )


async def test_event_approved_sets_review_and_label():
    await db_module.init_db()
    sid = await _new_suggestion()
    await record_suggestion_event(sid, "approved")
    row = await get_suggestion(sid)
    assert row["review_status"] == "approved"
    assert row["human_label"] == "accepted"
    assert row["effective_label"] == "accepted"


async def test_event_edited_then_sent_is_corrected():
    await db_module.init_db()
    sid = await _new_suggestion()
    await record_suggestion_event(sid, "edit_started")
    await record_suggestion_event(sid, "edited", payload="исправленный текст")
    await record_suggestion_event(sid, "send_requested")
    await record_suggestion_event(sid, "sent", payload="исправленный текст", hde_post_id="4321")
    row = await get_suggestion(sid)
    assert row["review_status"] == "edited"
    assert row["delivery_status"] == "sent"
    assert row["final_sent_text"] == "исправленный текст"
    assert row["final_sent_post_id"] == "4321"
    assert row["human_label"] == "corrected"


async def test_event_send_failed_leaves_label_none():
    await db_module.init_db()
    sid = await _new_suggestion()
    await record_suggestion_event(sid, "send_requested")
    await record_suggestion_event(sid, "send_failed", payload="HDE 500")
    row = await get_suggestion(sid)
    assert row["delivery_status"] == "failed"
    assert row["human_label"] is None
