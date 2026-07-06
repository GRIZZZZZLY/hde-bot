from __future__ import annotations

import difflib
import re
from typing import Iterable, Optional

import aiosqlite

from .core import connect


# ---------------------------------------------------------------------------
# Solution patterns (AI answer quality)
# ---------------------------------------------------------------------------

async def save_solution_pattern(
    problem_type: str,
    steps: str,
    source: str = "analyze",
    equipment: Optional[str] = None,
) -> int:
    """Insert a new solution pattern. Returns new row id."""
    async with connect() as db:
        cursor = await db.execute(
            "INSERT INTO solution_patterns (equipment, problem_type, steps, source) VALUES (?, ?, ?, ?)",
            (equipment, problem_type, steps, source),
        )
        await db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid


async def find_solution_pattern(
    equipment: Optional[str],
    keywords: str,
) -> Optional[dict]:
    """Return the best matching pattern for given equipment and keywords, or None."""
    words = {w for w in re.sub(r"[^\w\s]", " ", keywords.lower()).split() if len(w) >= 3}
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        candidates: list = []
        if equipment:
            async with db.execute(
                "SELECT * FROM solution_patterns WHERE equipment = ? ORDER BY use_count DESC LIMIT 10",
                (equipment,),
            ) as cur:
                candidates = list(await cur.fetchall())
        if not candidates:
            async with db.execute(
                "SELECT * FROM solution_patterns WHERE equipment IS NULL ORDER BY use_count DESC LIMIT 10",
            ) as cur:
                candidates = list(await cur.fetchall())
        if not candidates:
            return None
        best: Optional[dict] = None
        best_score = -1
        for row in candidates:
            row_words = set(row["problem_type"].lower().split())
            score = len(words & row_words)
            if score > best_score:
                best_score = score
                best = dict(row)
        # Only return if at least one keyword matched
        return best if best_score > 0 else None


async def increment_pattern_use(pattern_id: int) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE solution_patterns SET use_count = use_count + 1 WHERE id = ?",
            (pattern_id,),
        )
        await db.commit()


async def list_solution_patterns(limit: int = 50) -> list[dict]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM solution_patterns ORDER BY use_count DESC, created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


def build_pattern_index(patterns: Iterable[dict]) -> dict[Optional[str], list[str]]:
    """Group lowercased problem_type strings by equipment for in-memory dedup."""
    index: dict[Optional[str], list[str]] = {}
    for p in patterns:
        index.setdefault(p["equipment"], []).append(p["problem_type"].lower())
    return index


def pattern_similar_in_index(
    index: dict[Optional[str], list[str]],
    equipment: Optional[str],
    problem_type: str,
) -> bool:
    """Fuzzy-match problem_type against indexed patterns of the same equipment."""
    target = problem_type.lower()
    for existing in index.get(equipment, ()):
        if difflib.SequenceMatcher(None, existing, target).ratio() >= 0.7:
            return True
    return False


async def pattern_exists_similar(
    equipment: Optional[str],
    problem_type: str,
) -> bool:
    """Return True if a pattern with same equipment and similar problem_type exists.

    One-shot convenience wrapper. Batch callers (e.g. /aianalyze) should load
    patterns once via list_solution_patterns + build_pattern_index and call
    pattern_similar_in_index per candidate instead.
    """
    patterns = await list_solution_patterns(limit=200)
    return pattern_similar_in_index(build_pattern_index(patterns), equipment, problem_type)


async def count_solution_patterns_by_equipment() -> dict[str, int]:
    """Return {equipment_label: count} for reporting."""
    async with connect() as db:
        async with db.execute(
            "SELECT COALESCE(equipment, 'Без бренда'), COUNT(*) "
            "FROM solution_patterns GROUP BY equipment ORDER BY COUNT(*) DESC"
        ) as cur:
            return dict(await cur.fetchall())
