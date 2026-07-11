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


from bot.db.dialogue_store import (
    count_pairs_by_quality,
    list_fewshot_candidates,
    list_pairs_for_gating,
    set_pair_quality,
)


async def test_gating_queue_and_quality_update():
    await db_module.init_db()
    pid, _ = await save_dialogue_pair(
        ticket_id="T1", context="Клиент: вопрос", operator_answer="ответ",
        content_hash="hq1",
    )
    queue = await list_pairs_for_gating()
    assert any(r["pair_id"] == pid for r in queue)
    await set_pair_quality(pid, "auto_accepted", "полезный ответ")
    assert all(r["pair_id"] != pid for r in await list_pairs_for_gating())
    counts = await count_pairs_by_quality()
    assert counts.get("auto_accepted") == 1


async def test_gating_queue_prioritizes_own_operator():
    await db_module.init_db()
    # чужой ответ вставлен раньше (меньший pair_id), свой — позже
    other, _ = await save_dialogue_pair(
        ticket_id="T1", context="Клиент: a", operator_answer="чужой",
        content_hash="go1", operator_user_id="67",
    )
    own, _ = await save_dialogue_pair(
        ticket_id="T2", context="Клиент: b", operator_answer="мой",
        content_hash="go2", operator_user_id="98",
    )
    queue = await list_pairs_for_gating(own_operator_id="98")
    assert queue[0]["pair_id"] == own          # свой первым, несмотря на pair_id


async def test_fewshot_candidates_require_quality_and_embedding():
    await db_module.init_db()
    ok, _ = await save_dialogue_pair(
        ticket_id="T1", context="Клиент: касса не видит ККТ", operator_answer="проверьте USB",
        content_hash="hf1", operator_user_id="98",
        embedding=b"\x00\x01", embedding_status="ready",
    )
    await set_pair_quality(ok, "auto_accepted", None)
    # unreviewed с эмбеддингом — не кандидат
    await save_dialogue_pair(
        ticket_id="T2", context="Клиент: x", operator_answer="y",
        content_hash="hf2", embedding=b"\x00\x01", embedding_status="ready",
    )
    # accepted без эмбеддинга — не кандидат
    no_emb, _ = await save_dialogue_pair(
        ticket_id="T3", context="Клиент: z", operator_answer="w", content_hash="hf3",
    )
    await set_pair_quality(no_emb, "auto_accepted", None)

    rows = await list_fewshot_candidates()
    assert [r["pair_id"] for r in rows] == [ok]
    assert rows[0]["operator_user_id"] == "98"
    assert rows[0]["embedding"] == b"\x00\x01"
