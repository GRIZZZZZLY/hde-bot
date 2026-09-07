"""Очередь кандидатов в базу знаний: вердикты сверки, ждущие разбора.

Кандидат появляется, когда судья сказал bot_wrong_fact — бот ответил неверно по
существу, а оператор ответил иначе.

С 2026-09-07 очередь разбирается автоматически (см. agent/kb_distill.py):
кандидат проходит через выжимку в обобщаемое правило, и человеку показывается
единственный класс — противоречие с уже накопленным правилом. Остальные исходы
(auto_added / duplicate / not_generalizable) закрываются без него, потому что
решать по каждой строке руками невозможно: на проде это дало 19 pending при 2
решённых, и большинство строк правилом не являлись.
"""
from __future__ import annotations

import aiosqlite

from .core import connect

PENDING = "pending"
CONFLICT = "conflict"


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


async def list_conflict_kb_candidates(limit: int = 10) -> list[dict]:
    """Кандидаты, по которым нужен человек: новое правило спорит с накопленным.

    Отдельно от pending: pending — это «ещё не разобрано автоматом», conflict —
    «разобрано, и выбрать сторону может только человек».
    """
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM kb_candidates WHERE status=? ORDER BY id LIMIT ?",
            (CONFLICT, int(limit)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_kb_candidate_stats(hours: int = 24) -> dict[str, int]:
    """Исходы разбора очереди за окно — строка «+N правил» в утренней сводке."""
    async with connect() as db:
        async with db.execute(
            "SELECT status, COUNT(*) FROM kb_candidates "
            "WHERE decided_at >= datetime('now', ?) GROUP BY status",
            (f"-{int(hours)} hours",),
        ) as cur:
            return {str(row[0]): int(row[1]) for row in await cur.fetchall()}


async def set_kb_candidate_status(
    candidate_id: int, status: str, *, kind: str | None = None,
    rule_json: str | None = None, conflict_item_id: int | None = None,
) -> dict | None:
    """Решение по кандидату. None, если решение уже принято — второй клик по
    кнопке в Telegram не должен второй раз писать статью в базу.

    Переход разрешён только из pending, и это же служит защитой от повторного
    прохода джоба: первый вызов уводит строку из pending, второй вернёт None.
    Кандидат в статусе conflict решается через resolve_kb_conflict.
    """
    sets = ["status=?", "decided_at=datetime('now')"]
    params: list = [status]
    if kind is not None:
        sets.append("kind=?")
        params.append(kind)
    if rule_json is not None:
        sets.append("rule_json=?")
        params.append(rule_json)
    if conflict_item_id is not None:
        sets.append("conflict_item_id=?")
        params.append(int(conflict_item_id))
    params += [int(candidate_id), PENDING]

    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"UPDATE kb_candidates SET {', '.join(sets)} WHERE id=? AND status=?",
            tuple(params),
        )
        if not cursor.rowcount:
            return None
        await db.commit()
        async with db.execute(
            "SELECT * FROM kb_candidates WHERE id=?", (int(candidate_id),)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def resolve_kb_conflict(candidate_id: int, status: str) -> dict | None:
    """Решение человека по противоречию: из conflict в окончательный статус."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "UPDATE kb_candidates SET status=?, decided_at=datetime('now') "
            "WHERE id=? AND status=?",
            (status, int(candidate_id), CONFLICT),
        )
        if not cursor.rowcount:
            return None
        await db.commit()
        async with db.execute(
            "SELECT * FROM kb_candidates WHERE id=?", (int(candidate_id),)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None
