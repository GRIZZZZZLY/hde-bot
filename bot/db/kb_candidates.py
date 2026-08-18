"""Очередь кандидатов в базу знаний: вердикты сверки, ждущие решения человека.

Кандидат появляется, когда судья сказал bot_wrong_fact — бот ответил неверно по
существу, а оператор ответил иначе. Прямо в базу такое не пишем: категорию
поставила модель, и её ошибка иначе уехала бы в RAG без человеческого взгляда.
"""
from __future__ import annotations

import aiosqlite

from .core import connect

PENDING = "pending"


async def save_kb_candidate(
    *, suggestion_id: int, ticket_id: str, title: str, history: str,
    ai_answer: str, reference_answer: str, reason: str,
) -> int | None:
    """Ставит кандидата в очередь. None, если по этому предложению он уже есть."""
    async with connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO kb_candidates "
            "(suggestion_id, ticket_id, title, history, ai_answer, reference_answer, "
            " reason, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(suggestion_id), str(ticket_id), title or "", history or "",
             ai_answer or "", reference_answer or "", reason or "", PENDING),
        )
        await db.commit()
    return cursor.lastrowid if cursor.rowcount else None


async def list_pending_kb_candidates(limit: int = 10) -> list[dict]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM kb_candidates WHERE status=? ORDER BY id LIMIT ?",
            (PENDING, int(limit)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_kb_candidate(candidate_id: int) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM kb_candidates WHERE id=?", (int(candidate_id),)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def set_kb_candidate_status(candidate_id: int, status: str) -> dict | None:
    """Решение по кандидату. None, если решение уже принято — второй клик по
    кнопке в Telegram не должен второй раз писать статью в базу."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "UPDATE kb_candidates SET status=?, decided_at=datetime('now') "
            "WHERE id=? AND status=?",
            (status, int(candidate_id), PENDING),
        )
        if not cursor.rowcount:
            return None
        await db.commit()
        async with db.execute(
            "SELECT * FROM kb_candidates WHERE id=?", (int(candidate_id),)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None
