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


from bot.db.dialogue_store import (
    count_dialogue_pairs,
    dialogue_pair_hashes,
    list_pending_embeddings,
    list_processed_ticket_ids,
    mark_ticket_processed,
    save_dialogue_pair,
    set_pair_embedding,
)


async def test_save_returns_created_flag_and_is_idempotent():
    await db_module.init_db()
    kw = dict(ticket_id="T1", context="Клиент: q", operator_answer="a", content_hash="h1")
    pid1, created1 = await save_dialogue_pair(**kw)
    pid2, created2 = await save_dialogue_pair(**kw)
    assert created1 is True and created2 is False
    assert pid1 == pid2
    assert await count_dialogue_pairs() == 1
    assert "h1" in await dialogue_pair_hashes()


async def test_processed_cursor_roundtrip():
    await db_module.init_db()
    assert await list_processed_ticket_ids() == set()
    await mark_ticket_processed("T1")
    await mark_ticket_processed("T2")
    assert await list_processed_ticket_ids() == {"T1", "T2"}


async def test_pending_embedding_flow():
    await db_module.init_db()
    pid, _ = await save_dialogue_pair(
        ticket_id="T1", context="c", operator_answer="a", content_hash="h2",
        embedding=None, embedding_status="pending",
    )
    pend = await list_pending_embeddings()
    assert any(r["pair_id"] == pid for r in pend)
    await set_pair_embedding(pid, b"\x00\x01", "e5", "ready")
    pend2 = await list_pending_embeddings()
    assert all(r["pair_id"] != pid for r in pend2)
