import bot.db as db_module
from bot.db.suggestion_store import get_suggestion, record_suggestion
from bot.handlers.ai_feedback import _record_event


async def test_record_event_maps_topic_to_latest_suggestion():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="T", topic_id=42, trigger_source="first",
        context_until_post_id="5", pipeline_version="v0", prompt_version="legacy",
    )
    await _record_event(42, "approved")
    row = await get_suggestion(sid)
    assert row["review_status"] == "approved"
    assert row["human_label"] == "accepted"


async def test_record_event_noop_without_suggestion():
    await db_module.init_db()
    # no suggestion for this topic — must not raise
    await _record_event(999, "approved")
