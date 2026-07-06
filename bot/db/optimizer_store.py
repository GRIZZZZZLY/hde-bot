from __future__ import annotations

from datetime import datetime, timezone, timedelta

import aiosqlite

from .core import connect


# ---------------------------------------------------------------------------
# Prompt optimizer — optimization_samples and prompt_versions
# ---------------------------------------------------------------------------

async def save_optimization_sample(
    ticket_id: str,
    title: str,
    history: str,
    ai_answer: str,
    outcome: str,
    *,
    op_answer: str | None = None,
    confidence: int | None = None,
) -> int:
    """Save a labelled operator feedback sample for prompt optimization."""
    async with connect() as db:
        cursor = await db.execute(
            "INSERT INTO optimization_samples "
            "(ticket_id, title, history, ai_answer, op_answer, outcome, confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticket_id, title or "", history, ai_answer, op_answer, outcome, confidence),
        )
        await db.commit()
        return cursor.lastrowid


async def get_optimization_samples(days: int = 30) -> list[dict]:
    """Return optimization samples from the last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, ticket_id, title, history, ai_answer, op_answer, outcome, confidence "
            "FROM optimization_samples WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_active_prompt() -> str | None:
    """Return content of the active prompt version, or None if none applied yet."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT content FROM prompt_versions WHERE status='active' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return row["content"] if row else None


async def get_prompt_version(version_id: int) -> dict | None:
    """Return a single prompt version row by id."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, content, score, proposed_by, status, created_at FROM prompt_versions WHERE id=?",
            (version_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def save_prompt_version(content: str, score: float | None, proposed_by: str) -> int:
    """Save a candidate prompt version. Returns its id."""
    async with connect() as db:
        cursor = await db.execute(
            "INSERT INTO prompt_versions (content, score, proposed_by, status) VALUES (?,?,?,?)",
            (content, score, proposed_by, "candidate"),
        )
        await db.commit()
        return cursor.lastrowid


async def apply_prompt_version(version_id: int) -> None:
    """Mark version as active, all others as rejected."""
    async with connect() as db:
        await db.execute(
            "UPDATE prompt_versions SET status='rejected' WHERE status IN ('active', 'candidate')"
        )
        await db.execute(
            "UPDATE prompt_versions SET status='active', applied_at=datetime('now') WHERE id=?",
            (version_id,),
        )
        await db.commit()


async def reject_all_prompt_candidates() -> None:
    """Mark all candidate prompt versions as rejected."""
    async with connect() as db:
        await db.execute(
            "UPDATE prompt_versions SET status='rejected' WHERE status='candidate'"
        )
        await db.commit()


async def list_prompt_versions(limit: int = 8) -> list[dict]:
    """Return last *limit* prompt versions newest-first."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, content, score, proposed_by, status, created_at FROM prompt_versions"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]
