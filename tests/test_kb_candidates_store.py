"""Очередь кандидатов в базу знаний после перехода на ревью-по-исключению.

Человеку показывается ровно один класс — противоречие с уже накопленным
правилом. Остальные исходы (автодобавление, дубль, «не обобщается») закрываются
без него и видны только счётчиком в утренней сводке.
"""
import json


async def _queue(suggestion_id=1, ticket="T1"):
    import bot.db as db
    await db.init_db()
    return await db.save_kb_candidate(
        suggestion_id=suggestion_id, ticket_id=ticket, title="Тема",
        history="Клиент: ...", ai_answer="передадим специалисту",
        reference_answer="Обращайтесь в банк", reason="не тот адресат",
    )


async def test_status_accepts_new_outcomes():
    import bot.db as db
    for i, status in enumerate(
        ["auto_added", "duplicate", "not_generalizable", "conflict"], start=1
    ):
        cid = await _queue(suggestion_id=100 + i, ticket=f"T{i}")
        row = await db.set_kb_candidate_status(cid, status)
        assert row is not None and row["status"] == status


async def test_status_stores_rule_and_conflict_reference():
    import bot.db as db
    cid = await _queue(suggestion_id=200)
    rule = {"symptom": "QR на терминале", "rule": "генерирует ОФД", "action": "в ОФД"}
    row = await db.set_kb_candidate_status(
        cid, "conflict", rule_json=json.dumps(rule, ensure_ascii=False),
        conflict_item_id=5, kind="rule",
    )
    assert row["conflict_item_id"] == 5
    assert row["kind"] == "rule"
    assert "ОФД" in row["rule_json"]


async def test_second_decision_is_rejected():
    """Двойной клик по кнопке не должен писать статью в базу второй раз."""
    import bot.db as db
    cid = await _queue(suggestion_id=300)
    assert await db.set_kb_candidate_status(cid, "auto_added") is not None
    assert await db.set_kb_candidate_status(cid, "added") is None


async def test_conflicts_are_listed_separately_from_pending():
    import bot.db as db
    pending_id = await _queue(suggestion_id=400, ticket="PEND")
    conflict_id = await _queue(suggestion_id=401, ticket="CONF")
    await db.set_kb_candidate_status(conflict_id, "conflict", conflict_item_id=9)

    pending = await db.list_pending_kb_candidates(limit=10)
    conflicts = await db.list_conflict_kb_candidates(limit=10)
    assert [c["id"] for c in pending] == [pending_id]
    assert [c["id"] for c in conflicts] == [conflict_id]


async def test_kb_outcome_stats_group_by_status():
    import bot.db as db
    for i, status in enumerate(["auto_added", "auto_added", "duplicate"], start=1):
        cid = await _queue(suggestion_id=500 + i, ticket=f"S{i}")
        await db.set_kb_candidate_status(cid, status)
    stats = await db.get_kb_candidate_stats(hours=24)
    assert stats.get("auto_added") == 2
    assert stats.get("duplicate") == 1


# --- сводка -----------------------------------------------------------------


def test_digest_reports_counters_not_ten_buttons():
    """Утро должно читаться одной строкой: десять сообщений с кнопками — это
    работа, которую невозможно доделать (19 pending при 2 решённых)."""
    from bot.formatter import format_kb_outcome_line
    line = format_kb_outcome_line({"auto_added": 4, "duplicate": 2,
                                   "not_generalizable": 3, "conflict": 1})
    assert "4" in line and "правил" in line.lower()
    assert "1" in line and "противореч" in line.lower()


def test_digest_kb_line_silent_when_nothing_happened():
    from bot.formatter import format_kb_outcome_line
    assert format_kb_outcome_line({}) is None
    assert format_kb_outcome_line({"auto_added": 0}) is None


def test_digest_kb_line_without_conflicts_omits_the_call_to_action():
    from bot.formatter import format_kb_outcome_line
    line = format_kb_outcome_line({"auto_added": 2})
    assert "противореч" not in line.lower()


def test_conflict_keyboard_offers_both_sides():
    """Противоречие — выбор между новым правилом и уже лежащим в базе, а не
    «добавить / мимо»: «мимо» здесь оставляет базу с неверной строкой."""
    from bot.handlers.ai_feedback import kb_conflict_kb
    buttons = [b for row in kb_conflict_kb(7).inline_keyboard for b in row]
    data = {b.callback_data for b in buttons}
    assert data == {"kbc:new:7", "kbc:old:7"}


# --- архивация --------------------------------------------------------------


async def test_archive_marks_unused_auto_rules():
    """Правило, которое N дней не всплывало в retrieval, — шум, а не знание.
    Не удаляем: строка нужна для разбора, но из поиска уходит."""
    import bot.db as db
    from bot.db.core import connect
    await db.init_db()

    fresh = await db.upsert_knowledge_item(
        source="auto_rule", content="Правило: свежее", quality="auto_rule",
    )
    stale = await db.upsert_knowledge_item(
        source="auto_rule", content="Правило: забытое", quality="auto_rule",
    )
    other = await db.upsert_knowledge_item(
        source="hde_closed", content="обычный тикет", quality="good",
    )
    async with connect() as conn:
        await conn.execute(
            "UPDATE knowledge_items SET created_at=datetime('now', '-90 days') "
            "WHERE id IN (?, ?)", (stale[0], other[0]),
        )
        await conn.execute(
            "UPDATE knowledge_items SET last_used_at=datetime('now') WHERE id=?",
            (fresh[0],),
        )
        await conn.commit()

    archived = await db.archive_unused_auto_rules(days=60)
    assert archived == 1
    async with connect() as conn:
        conn.row_factory = None
        async with conn.execute(
            "SELECT id, quality FROM knowledge_items ORDER BY id"
        ) as cur:
            rows = dict(await cur.fetchall())
    assert rows[stale[0]] == "archived"
    assert rows[fresh[0]] == "auto_rule"
    assert rows[other[0]] == "good"        # чужой источник не трогаем


async def test_archived_rules_leave_retrieval():
    """Смысл архивации: правило перестаёт попадать в промпт."""
    import numpy as np
    import bot.db as db
    from bot.db.core import connect
    from bot.knowledge.store import find_similar, invalidate_embeddings_cache
    await db.init_db()

    emb = np.ones(4, dtype=np.float32)
    item_id, _ = await db.upsert_knowledge_item(
        source="auto_rule", content="Правило: архивное", quality="auto_rule",
    )
    await db.update_knowledge_embedding(item_id, emb.tobytes())
    invalidate_embeddings_cache()
    assert any(i.id == item_id for i, _ in await find_similar(emb, limit=5))

    async with connect() as conn:
        await conn.execute(
            "UPDATE knowledge_items SET quality='archived' WHERE id=?", (item_id,)
        )
        await conn.commit()
    invalidate_embeddings_cache()
    assert all(i.id != item_id for i, _ in await find_similar(emb, limit=5))
