import bot.db as db_module
from bot.db.suggestion_store import compute_idempotency_key, derive_human_label


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
