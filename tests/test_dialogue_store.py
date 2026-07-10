import bot.db as db_module


async def _columns(table: str) -> set[str]:
    from bot.db.core import connect
    async with connect() as db:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
    return {r[1] for r in rows}


async def test_dialogue_pairs_table_created():
    await db_module.init_db()
    cols = await _columns("dialogue_pairs")
    expected = {
        "pair_id", "ticket_id", "source_message_id", "context_until_message_id",
        "operator_message_id", "context", "operator_answer", "issue_type",
        "client_id", "operator_answer_at", "resolved_at", "inserted_at",
        "resolution_status", "quality_status", "quality_reason",
        "embedding", "embedding_model", "embedding_status", "embedding_text_hash",
        "content_hash",
    }
    assert expected <= cols
