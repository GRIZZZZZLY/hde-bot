from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite

from .core import connect, logger


# ---------------------------------------------------------------------------
# Knowledge items
# ---------------------------------------------------------------------------

async def save_knowledge_item(
    source: str,
    content: str,
    *,
    ticket_id: str = "",
    title: str = "",
    embedding: bytes | None = None,
    quality: str = "good",
    url: str = "",
    content_hash: str = "",
    company_id: str = "",
    company_name: str = "",
) -> int:
    """Insert a new knowledge item. Returns the new row id."""
    async with connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO knowledge_items
                (source, ticket_id, title, content, embedding, quality, url,
                 content_hash, company_id, company_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, ticket_id or None, title or None, content,
             embedding, quality, url or None, content_hash or None,
             company_id or None, company_name or None),
        )
        if quality != "bad" and cursor.lastrowid:
            try:
                await db.execute(
                    "INSERT INTO knowledge_fts(rowid, content) VALUES (?, ?)",
                    (cursor.lastrowid, content),
                )
            except Exception:
                pass  # FTS insert failure is non-fatal
        await db.commit()
        row_id = cursor.lastrowid
        assert row_id is not None, "INSERT into knowledge_items returned no lastrowid"
        return row_id


async def list_knowledge_content_hashes() -> set[str]:
    """Return all non-null content_hash values in one query (bulk dedup)."""
    async with connect() as db:
        async with db.execute(
            "SELECT content_hash FROM knowledge_items WHERE content_hash IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
    return {row[0] for row in rows}


async def update_knowledge_embedding(item_id: int, embedding: bytes) -> None:
    """Store the embedding blob for an existing knowledge item."""
    async with connect() as db:
        await db.execute(
            "UPDATE knowledge_items SET embedding = ? WHERE id = ?",
            (embedding, item_id),
        )
        await db.commit()


async def update_knowledge_embeddings(pairs: list[tuple[int, bytes]]) -> None:
    """Store embedding blobs for many knowledge items in one transaction."""
    if not pairs:
        return
    async with connect() as db:
        await db.executemany(
            "UPDATE knowledge_items SET embedding = ? WHERE id = ?",
            [(embedding, item_id) for item_id, embedding in pairs],
        )
        await db.commit()


async def list_knowledge_items_without_embedding() -> list[tuple[int, str]]:
    """Return (id, content) for rows missing an embedding."""
    async with connect() as db:
        async with db.execute(
            "SELECT id, content FROM knowledge_items WHERE embedding IS NULL AND quality NOT IN ('bad', 'expired')"
        ) as cur:
            return await cur.fetchall()


async def list_all_knowledge_embeddings() -> list[tuple[int, str, bytes, str, str]]:
    """Return (id, content, embedding, company_id, source) for all indexed items."""
    async with connect() as db:
        async with db.execute(
            # 'archived' — автоправило, которое N дней не всплывало в retrieval
            # (archive_unused_auto_rules). Строка остаётся для разбора, но из
            # поиска уходит: иначе архивация ничего бы не давала.
            "SELECT id, content, embedding, COALESCE(company_id, ''), source "
            "FROM knowledge_items "
            "WHERE embedding IS NOT NULL "
            "  AND quality NOT IN ('bad', 'expired', 'archived')"
        ) as cur:
            return await cur.fetchall()


async def count_knowledge_by_source() -> dict[str, int]:
    """Return {source: count} statistics."""
    async with connect() as db:
        async with db.execute(
            "SELECT source, COUNT(*) FROM knowledge_items GROUP BY source"
        ) as cur:
            rows = await cur.fetchall()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# AI feedback pending (awaiting ✏️ correction)
# ---------------------------------------------------------------------------

async def save_ai_feedback_pending(
    topic_id: int,
    ticket_id: str,
    history: str,
    title: str,
    expires_at: str,
    answer_text: str = "",
    ai_full_text: str = "",
) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO ai_feedback_pending
                (topic_id, ticket_id, history, title, answer_text, ai_full_text, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (topic_id, ticket_id, history, title, answer_text, ai_full_text, expires_at),
        )
        await db.commit()


async def get_ai_feedback_pending(topic_id: int) -> Optional[dict]:
    """Return pending correction state or None if expired/missing."""
    async with connect() as db:
        async with db.execute(
            "SELECT ticket_id, history, title, answer_text, ai_full_text, expires_at "
            "FROM ai_feedback_pending WHERE topic_id = ?",
            (topic_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    from datetime import datetime, timezone
    expires = datetime.fromisoformat(row[5])
    if datetime.now(timezone.utc) > expires:
        await delete_ai_feedback_pending(topic_id)
        return None
    return {
        "ticket_id": row[0],
        "history": row[1],
        "title": row[2],
        "answer_text": row[3],
        "ai_full_text": row[4] or row[3],
    }


async def delete_ai_feedback_pending(topic_id: int) -> None:
    async with connect() as db:
        await db.execute(
            "DELETE FROM ai_feedback_pending WHERE topic_id = ?", (topic_id,)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# AI status helpers
# ---------------------------------------------------------------------------

async def count_items_without_embedding() -> int:
    """Count knowledge_items that have no embedding blob (failed or pending)."""
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items "
            "WHERE embedding IS NULL AND quality NOT IN ('bad', 'expired')"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_last_knowledge_item_date() -> str | None:
    """Return ISO timestamp of the most recently created knowledge_item, or None."""
    async with connect() as db:
        async with db.execute(
            "SELECT MAX(created_at) FROM knowledge_items"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None


async def list_items_without_company() -> list[tuple[int, str]]:
    """Return (id, ticket_id) for hde_closed items missing company_name."""
    async with connect() as db:
        async with db.execute(
            "SELECT id, ticket_id FROM knowledge_items "
            "WHERE source = 'hde_closed' "
            "AND (company_name IS NULL OR company_name = '') "
            "AND ticket_id IS NOT NULL"
        ) as cur:
            return await cur.fetchall()


async def update_knowledge_company(item_id: int, company_id: str, company_name: str) -> None:
    """Set company_id and company_name for an existing knowledge item."""
    async with connect() as db:
        await db.execute(
            "UPDATE knowledge_items SET company_id = ?, company_name = ? WHERE id = ?",
            (company_id or None, company_name or None, item_id),
        )
        await db.commit()


_KB_TOKEN_RE = None


async def fts_search_source_any_token(
    query: str, source: str, limit: int = 10
) -> list[int]:
    """BM25-поиск внутри одного источника: OR по значимым токенам запроса.

    Отличие от fts_search_knowledge: там запрос уходит в MATCH как есть, а в
    FTS5 пробел — это неявный AND, поэтому на длинном запросе (титул + хвост
    истории) совпадений почти не бывает. Здесь нужен именно лексический
    «якорь» — хотя бы одно осмысленное слово тикета должно встречаться в чанке.
    """
    global _KB_TOKEN_RE
    if _KB_TOKEN_RE is None:
        import re as _re
        _KB_TOKEN_RE = _re.compile(r"[\wа-яёА-ЯЁ]{4,}")
    tokens = {t.lower() for t in _KB_TOKEN_RE.findall(query or "")}
    if not tokens:
        return []
    expr = " OR ".join(f'"{t}"' for t in sorted(tokens))
    async with connect() as db:
        try:
            async with db.execute(
                "SELECT f.rowid FROM knowledge_fts f "
                "JOIN knowledge_items k ON k.id = f.rowid "
                "WHERE f.content MATCH ? AND k.source = ? "
                "ORDER BY rank LIMIT ?",
                (expr, source, limit),
            ) as cur:
                return [int(row[0]) for row in await cur.fetchall()]
        except Exception:
            return []


async def fts_search_knowledge(query: str, limit: int = 10) -> list[tuple[int, str]]:
    """BM25 full-text search via FTS5. Returns (id, content) ordered by relevance."""
    import re
    # Strip FTS5 special characters to avoid syntax errors
    clean = re.sub(r'["\(\)\^\*\-]', ' ', query).strip()
    if not clean:
        return []
    async with connect() as db:
        try:
            async with db.execute(
                "SELECT rowid, content FROM knowledge_fts WHERE content MATCH ? ORDER BY rank LIMIT ?",
                (clean, limit),
            ) as cur:
                return [(int(row[0]), row[1]) for row in await cur.fetchall()]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Knowledge management — upsert, dedup, expiry, metrics
# ---------------------------------------------------------------------------

async def upsert_knowledge_item(
    source: str,
    content: str,
    *,
    ticket_id: str = "",
    title: str = "",
    quality: str = "good",
    url: str = "",
    content_hash: str = "",
    company_id: str = "",
    company_name: str = "",
) -> tuple[int, bool]:
    """Insert or update knowledge item by ticket_id+source. Returns (id, was_created).

    If ticket_id is provided and a row with same (ticket_id, source) exists:
    -> UPDATE content, title, reset embedding=NULL (triggers re-embedding), update FTS.
    Otherwise -> INSERT new row.
    """
    async with connect() as db:
        if ticket_id:
            async with db.execute(
                "SELECT id FROM knowledge_items WHERE ticket_id = ? AND source = ?",
                (ticket_id, source),
            ) as cur:
                row = await cur.fetchone()
            if row:
                item_id = row[0]
                await db.execute(
                    "UPDATE knowledge_items "
                    "SET content=?, title=?, quality=?, url=?, content_hash=?, "
                    "company_id=?, company_name=?, embedding=NULL, "
                    "created_at=datetime('now') "
                    "WHERE id=?",
                    (content, title or None, quality, url or None,
                     content_hash or None,
                     company_id or None, company_name or None, item_id),
                )
                # Обновить FTS
                await db.execute(
                    "DELETE FROM knowledge_fts WHERE rowid=?", (item_id,)
                )
                await db.execute(
                    "INSERT INTO knowledge_fts(rowid, content) VALUES (?, ?)",
                    (item_id, content),
                )
                await db.commit()
                return item_id, False
        # INSERT
        cursor = await db.execute(
            "INSERT INTO knowledge_items "
            "(source, ticket_id, title, content, quality, url, content_hash, "
            "company_id, company_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, ticket_id or None, title or None, content, quality,
             url or None, content_hash or None,
             company_id or None, company_name or None),
        )
        item_id = cursor.lastrowid
        if item_id is None:
            raise RuntimeError("INSERT into knowledge_items returned no lastrowid")
        try:
            await db.execute(
                "INSERT OR IGNORE INTO knowledge_fts(rowid, content) VALUES (?, ?)",
                (item_id, content),
            )
        except Exception as exc:
            logger.warning("FTS insert failed for item %s: %s", item_id, exc)
        await db.commit()
        return item_id, True


async def delete_knowledge_item_by_ticket(ticket_id: str, source: str) -> int:
    """Delete knowledge items by ticket_id and source. Also cleans FTS. Returns count deleted."""
    async with connect() as db:
        async with db.execute(
            "SELECT id FROM knowledge_items WHERE ticket_id = ? AND source = ?",
            (ticket_id, source),
        ) as cur:
            rows = await cur.fetchall()
        ids = [r[0] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            await db.execute(
                f"DELETE FROM knowledge_fts WHERE rowid IN ({placeholders})", ids
            )
            await db.execute(
                f"DELETE FROM knowledge_items WHERE id IN ({placeholders})", ids
            )
            await db.commit()
        return len(ids)


async def dedup_knowledge_items() -> int:
    """Mark duplicate items (same ticket_id+source, keep newest id) as quality='bad'.
    Returns count of newly marked duplicates."""
    async with connect() as db:
        async with db.execute(
            """
            SELECT id FROM knowledge_items
            WHERE ticket_id IS NOT NULL
              AND ticket_id != ''
              AND quality NOT IN ('bad', 'expired')
              AND id NOT IN (
                  SELECT MAX(id)
                  FROM knowledge_items
                  WHERE ticket_id IS NOT NULL AND ticket_id != ''
                    AND quality NOT IN ('bad', 'expired')
                  GROUP BY ticket_id, source
              )
            """
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return 0
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE knowledge_items SET quality='bad' WHERE id IN ({placeholders})", ids
        )
        await db.commit()
        return len(ids)


async def expire_stale_knowledge(expiry_days: int = 180) -> int:
    """Mark old unused hde_closed items as quality='expired'. Returns count marked."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=expiry_days)).isoformat()
    async with connect() as db:
        cur = await db.execute(
            """
            UPDATE knowledge_items
            SET quality = 'expired'
            WHERE source = 'hde_closed'
              AND quality = 'good'
              AND created_at < ?
              AND (last_used_at IS NULL OR last_used_at < ?)
            """,
            (cutoff, cutoff),
        )
        await db.commit()
        return cur.rowcount


async def retire_knowledge_item(item_id: int) -> bool:
    """Снять одну статью с поиска, не удаляя её.

    Нужно, когда оператор решил противоречие в пользу нового правила: старое
    больше не должно попадать в промпт, но должно остаться читаемым — по нему
    видно, что именно модель выдумала.
    """
    async with connect() as db:
        cur = await db.execute(
            "UPDATE knowledge_items SET quality='archived' WHERE id=?", (int(item_id),)
        )
        await db.commit()
        return bool(cur.rowcount)


async def archive_unused_auto_rules(days: int = 60) -> int:
    """Убрать из поиска автоправила, которые столько дней не всплывали.

    Автоправило пишет модель, и его ценность подтверждается только тем, что
    retrieval его находит. Не найденное за два месяца — шум, который засоряет
    слоты промпта. Не удаляем: строка нужна, чтобы понять, что модель выдумала,
    и вернуть правило вручную, если оно всё же верное.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with connect() as db:
        cur = await db.execute(
            """
            UPDATE knowledge_items
            SET quality = 'archived'
            WHERE source = 'auto_rule'
              AND quality = 'auto_rule'
              AND created_at < ?
              AND (last_used_at IS NULL OR last_used_at < ?)
            """,
            (cutoff, cutoff),
        )
        await db.commit()
        return cur.rowcount


async def mark_knowledge_items_analyzed(item_ids: list[int]) -> None:
    """Set analyzed_at = now for the given knowledge_item ids."""
    if not item_ids:
        return
    placeholders = ",".join("?" * len(item_ids))
    async with connect() as db:
        await db.execute(
            f"UPDATE knowledge_items SET analyzed_at = datetime('now') WHERE id IN ({placeholders})",
            item_ids,
        )
        await db.commit()


async def update_knowledge_last_used(item_ids: list[int]) -> None:
    """Update last_used_at for items where it's NULL or older than 24 hours (throttled)."""
    if not item_ids:
        return
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    placeholders = ",".join("?" * len(item_ids))
    async with connect() as db:
        await db.execute(
            f"UPDATE knowledge_items SET last_used_at = datetime('now') "
            f"WHERE id IN ({placeholders}) "
            f"AND (last_used_at IS NULL OR last_used_at < ?)",
            (*item_ids, cutoff_24h),
        )
        await db.commit()


async def get_knowledge_metrics() -> dict:
    """Return metrics for /aimetrics command."""
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async with connect() as db:
        # by_source (только active)
        async with db.execute(
            "SELECT source, COUNT(*) FROM knowledge_items "
            "WHERE quality NOT IN ('bad', 'expired') GROUP BY source"
        ) as cur:
            by_source = dict(await cur.fetchall())
        # expired count
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE quality = 'expired'"
        ) as cur:
            expired_count = (await cur.fetchone())[0]
        # without embedding
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items "
            "WHERE quality NOT IN ('bad', 'expired') AND embedding IS NULL"
        ) as cur:
            no_embedding_count = (await cur.fetchone())[0]
        # top 5 solution_patterns by use_count
        try:
            async with db.execute(
                "SELECT equipment, problem_type, use_count "
                "FROM solution_patterns ORDER BY use_count DESC LIMIT 5"
            ) as cur:
                top_patterns = [
                    {"equipment": r[0], "problem_type": r[1], "use_count": r[2]}
                    for r in await cur.fetchall()
                ]
        except Exception:
            top_patterns = []
        # top 5 "dead" items: good, never used, older than 30 days
        async with db.execute(
            "SELECT id, title, created_at, source FROM knowledge_items "
            "WHERE quality = 'good' AND last_used_at IS NULL AND created_at < ? "
            "ORDER BY created_at ASC LIMIT 5",
            (cutoff_30d,),
        ) as cur:
            dead_items = [
                {"id": r[0], "title": r[1], "created_at": r[2], "source": r[3]}
                for r in await cur.fetchall()
            ]
    return {
        "by_source": by_source,
        "total": sum(by_source.values()),
        "expired_count": expired_count,
        "no_embedding_count": no_embedding_count,
        "top_patterns": top_patterns,
        "dead_items": dead_items,
    }
