"""Store для dialogue_pairs (Phase 2A). Курсор по обработанным ticket_id,
очередь pending-эмбеддингов, журнал ошибок тикетов."""
from __future__ import annotations

import json

import aiosqlite

from .core import connect
from .misc import get_setting, set_setting

_PROCESSED_KEY = "dialogue_processed_ids"
_ERRORS_KEY = "dialogue_mining_errors"


async def save_dialogue_pair(
    *,
    ticket_id: str,
    context: str,
    operator_answer: str,
    content_hash: str,
    source_message_id: str | None = None,
    context_until_message_id: str | None = None,
    operator_message_id: str | None = None,
    issue_type: str | None = None,
    client_id: str | None = None,
    operator_answer_at: str | None = None,
    resolved_at: str | None = None,
    resolution_status: str | None = None,
    embedding: bytes | None = None,
    embedding_model: str | None = None,
    embedding_status: str = "pending",
    embedding_text_hash: str | None = None,
) -> tuple[int, bool]:
    """Вставляет пару идемпотентно по content_hash. Возвращает (pair_id, created)."""
    async with connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO dialogue_pairs "
            "(ticket_id, source_message_id, context_until_message_id, "
            " operator_message_id, context, operator_answer, issue_type, client_id, "
            " operator_answer_at, resolved_at, resolution_status, embedding, "
            " embedding_model, embedding_status, embedding_text_hash, content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_id, source_message_id, context_until_message_id,
                operator_message_id, context, operator_answer, issue_type, client_id,
                operator_answer_at, resolved_at, resolution_status, embedding,
                embedding_model, embedding_status, embedding_text_hash, content_hash,
            ),
        )
        created = cursor.rowcount > 0
        await db.commit()
        async with db.execute(
            "SELECT pair_id FROM dialogue_pairs WHERE content_hash=?", (content_hash,)
        ) as cur:
            row = await cur.fetchone()
    return (int(row[0]) if row else 0, created)


async def count_dialogue_pairs() -> int:
    async with connect() as db:
        async with db.execute("SELECT COUNT(*) FROM dialogue_pairs") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def dialogue_pair_hashes() -> set[str]:
    async with connect() as db:
        async with db.execute(
            "SELECT content_hash FROM dialogue_pairs WHERE content_hash IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def list_processed_ticket_ids() -> set[str]:
    raw = await get_setting(_PROCESSED_KEY, "")
    return set(json.loads(raw)) if raw else set()


async def mark_ticket_processed(ticket_id: str) -> None:
    current = await list_processed_ticket_ids()
    current.add(str(ticket_id))
    await set_setting(_PROCESSED_KEY, json.dumps(sorted(current)))


async def list_pending_embeddings(limit: int = 200) -> list[dict]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pair_id, context, operator_answer, embedding_text_hash "
            "FROM dialogue_pairs WHERE embedding_status IN ('pending','failed') "
            "LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_pair_embedding(
    pair_id: int, embedding: bytes | None, model: str | None, status: str
) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE dialogue_pairs SET embedding=?, embedding_model=?, "
            "embedding_status=? WHERE pair_id=?",
            (embedding, model, status, pair_id),
        )
        await db.commit()


async def log_ticket_error(ticket_id: str, error: str) -> None:
    raw = await get_setting(_ERRORS_KEY, "")
    errors = json.loads(raw) if raw else {}
    errors[str(ticket_id)] = error[:300]
    await set_setting(_ERRORS_KEY, json.dumps(errors, ensure_ascii=False))
