from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

from .time_utils import to_storage, utcnow

DB_PATH = "hde_bot.db"

TICKET_TOPIC_COLUMNS = {
    "unique_id": "TEXT DEFAULT ''",
    "company_name": "TEXT DEFAULT ''",
    "priority": "TEXT DEFAULT ''",
    "status": "TEXT DEFAULT ''",
    "owner_id": "TEXT DEFAULT ''",
    "owner_name": "TEXT DEFAULT ''",
    "topic_state": "TEXT DEFAULT 'active'",
    "delete_after_at": "TEXT",
    "last_client_reply_at": "TEXT",
    "last_staff_reply_at": "TEXT",
    "pre_sla_notify_at": "TEXT",
    "pre_sla_sent_at": "TEXT",
    "hde_link": "TEXT DEFAULT ''",
    "updated_at": "TEXT",
    "deleted_at": "TEXT",
    "last_assigned_at": "TEXT",
}

UPDATABLE_FIELDS = {
    "unique_id",
    "topic_id",
    "company_name",
    "ticket_name",
    "priority",
    "status",
    "owner_id",
    "owner_name",
    "topic_state",
    "delete_after_at",
    "last_client_reply_at",
    "last_staff_reply_at",
    "pre_sla_notify_at",
    "pre_sla_sent_at",
    "hde_link",
    "deleted_at",
    "last_assigned_at",
}


@dataclass
class TicketTopic:
    ticket_id: str
    unique_id: str
    topic_id: int
    company_name: str
    ticket_name: str
    priority: str
    status: str
    owner_id: str
    owner_name: str
    topic_state: str
    delete_after_at: Optional[str]
    last_client_reply_at: Optional[str]
    last_staff_reply_at: Optional[str]
    pre_sla_notify_at: Optional[str]
    pre_sla_sent_at: Optional[str]
    hde_link: str
    created_at: str
    updated_at: str
    deleted_at: Optional[str]
    last_assigned_at: Optional[str]

    @property
    def is_active(self) -> bool:
        return self.topic_state == "active"

    @property
    def is_pending_delete(self) -> bool:
        return self.topic_state == "pending_delete"

    @property
    def is_deleted(self) -> bool:
        return self.topic_state == "deleted"


@dataclass
class ReplyDraft:
    topic_id: int
    ticket_id: str
    text: str
    created_by: int
    created_at: str
    updated_at: str


@dataclass
class SentHdeMessage:
    telegram_message_id: int
    topic_id: int
    ticket_id: str
    hde_entity_id: int      # numeric ID in HDE (from response data.id)
    entity_type: str        # 'post' (public reply) or 'comment' (internal note)
    created_at: str


@dataclass
class CachedTopicMedia:
    topic_id: int
    message_id: int
    media_group_id: Optional[str]
    attachment_kind: str
    file_id: str
    filename: str
    content_type: str
    text: str
    created_at: str


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_topics (
                ticket_id   TEXT PRIMARY KEY,
                topic_id    INTEGER NOT NULL,
                company     TEXT DEFAULT '',
                ticket_name TEXT DEFAULT '',
                is_closed   INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now')),
                closed_at   TEXT
            )
            """
        )
        await _ensure_ticket_topic_columns(db)
        await _migrate_legacy_ticket_topics(db)

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_events (
                event_key   TEXT PRIMARY KEY,
                event_type  TEXT NOT NULL,
                ticket_id   TEXT NOT NULL,
                received_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reply_drafts (
                topic_id    INTEGER PRIMARY KEY,
                ticket_id   TEXT NOT NULL,
                text        TEXT NOT NULL,
                created_by  INTEGER NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_media_cache (
                topic_id        INTEGER NOT NULL,
                message_id      INTEGER NOT NULL,
                media_group_id  TEXT,
                attachment_kind TEXT NOT NULL,
                file_id         TEXT NOT NULL,
                filename        TEXT DEFAULT '',
                content_type    TEXT DEFAULT '',
                text            TEXT DEFAULT '',
                created_at      TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (topic_id, message_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_hde_messages (
                telegram_message_id  INTEGER NOT NULL,
                topic_id             INTEGER NOT NULL,
                ticket_id            TEXT NOT NULL,
                hde_entity_id        INTEGER NOT NULL,
                entity_type          TEXT NOT NULL,
                created_at           TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (telegram_message_id, topic_id)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_state
            ON ticket_topics(topic_state)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_delete_after
            ON ticket_topics(topic_state, delete_after_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pre_sla
            ON ticket_topics(topic_state, pre_sla_notify_at, pre_sla_sent_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_media_group
            ON topic_media_cache(topic_id, media_group_id, message_id)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS unassigned_general_messages (
                ticket_id   TEXT PRIMARY KEY,
                message_id  INTEGER NOT NULL,
                ticket_name TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS report_runs (
                report_date TEXT PRIMARY KEY,
                ran_at      TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await db.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                content,
                tokenize='unicode61 remove_diacritics 1'
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source       TEXT NOT NULL,
                ticket_id    TEXT,
                title        TEXT,
                content      TEXT NOT NULL,
                embedding    BLOB,
                quality      TEXT NOT NULL DEFAULT 'good',
                url          TEXT,
                content_hash TEXT,
                company_id   TEXT,
                company_name TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
            """
        )
        # Migrations: add columns that may be missing in existing DBs
        for col, col_type in [("company_id", "TEXT"), ("company_name", "TEXT")]:
            try:
                await db.execute(f"ALTER TABLE knowledge_items ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # column already exists
        # Migration: add last_used_at to knowledge_items if missing
        try:
            await db.execute(
                "ALTER TABLE knowledge_items ADD COLUMN last_used_at TEXT"
            )
        except Exception:
            pass  # column already exists

        # Populate FTS index for existing items (first-time migration, idempotent)
        try:
            await db.execute(
                """
                INSERT INTO knowledge_fts(rowid, content)
                SELECT id, content FROM knowledge_items
                WHERE quality NOT IN ('bad', 'expired')
                  AND id NOT IN (SELECT rowid FROM knowledge_fts)
                """
            )
        except Exception:
            pass  # FTS may already be populated or query not supported

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_feedback_pending (
                topic_id    INTEGER PRIMARY KEY,
                ticket_id   TEXT NOT NULL,
                history     TEXT NOT NULL,
                title       TEXT NOT NULL DEFAULT '',
                answer_text TEXT NOT NULL DEFAULT '',
                expires_at  TEXT NOT NULL
            )
            """
        )
        # Migration: add answer_text if missing in existing DBs
        try:
            await db.execute(
                "ALTER TABLE ai_feedback_pending ADD COLUMN answer_text TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass  # column already exists
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS solution_patterns (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                equipment    TEXT,
                problem_type TEXT NOT NULL,
                steps        TEXT NOT NULL,
                source       TEXT NOT NULL DEFAULT 'analyze',
                use_count    INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solution_patterns_equip
            ON solution_patterns(equipment, problem_type)
            """
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS optimization_samples (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   TEXT NOT NULL,
                title       TEXT,
                history     TEXT NOT NULL,
                ai_answer   TEXT NOT NULL,
                op_answer   TEXT,
                outcome     TEXT NOT NULL,
                confidence  INTEGER,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT NOT NULL,
                score       REAL,
                proposed_by TEXT,
                status      TEXT NOT NULL DEFAULT 'candidate',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                applied_at  TEXT
            )
        """)
        await db.commit()


async def _ensure_ticket_topic_columns(db: aiosqlite.Connection) -> None:
    columns = await _table_columns(db, "ticket_topics")
    for name, ddl in TICKET_TOPIC_COLUMNS.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE ticket_topics ADD COLUMN {name} {ddl}")


async def _table_columns(db: aiosqlite.Connection, table_name: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _migrate_legacy_ticket_topics(db: aiosqlite.Connection) -> None:
    columns = await _table_columns(db, "ticket_topics")

    if "company" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET company_name = COALESCE(NULLIF(company_name, ''), company, '')
            """
        )

    if "unique_id" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET unique_id = COALESCE(NULLIF(unique_id, ''), ticket_id)
            """
        )

    if "updated_at" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET updated_at = COALESCE(updated_at, created_at, datetime('now'))
            """
        )

    if "topic_state" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET topic_state = CASE
                WHEN topic_state IN ('active', 'pending_delete', 'deleted') THEN topic_state
                WHEN is_closed = 1 THEN 'deleted'
                ELSE 'active'
            END
            """
        )

    if "deleted_at" in columns and "closed_at" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET deleted_at = CASE
                WHEN topic_state = 'deleted' THEN COALESCE(deleted_at, closed_at, datetime('now'))
                ELSE deleted_at
            END
            """
        )


def _row_to_topic(row: aiosqlite.Row) -> TicketTopic:
    return TicketTopic(
        ticket_id=row["ticket_id"],
        unique_id=row["unique_id"] or row["ticket_id"],
        topic_id=row["topic_id"],
        company_name=row["company_name"] or "",
        ticket_name=row["ticket_name"],
        priority=row["priority"] or "",
        status=row["status"] or "",
        owner_id=row["owner_id"] or "",
        owner_name=row["owner_name"] or "",
        topic_state=row["topic_state"] or "active",
        delete_after_at=row["delete_after_at"],
        last_client_reply_at=row["last_client_reply_at"],
        last_staff_reply_at=row["last_staff_reply_at"],
        pre_sla_notify_at=row["pre_sla_notify_at"],
        pre_sla_sent_at=row["pre_sla_sent_at"],
        hde_link=row["hde_link"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        last_assigned_at=row["last_assigned_at"],
    )


async def get_topic(ticket_id: str) -> Optional[TicketTopic]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_topics WHERE ticket_id = ?",
            (ticket_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_topic(row) if row else None


async def get_topic_by_topic_id(topic_id: int) -> Optional[TicketTopic]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_topics WHERE topic_id = ?",
            (topic_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_topic(row) if row else None


async def upsert_topic(
    ticket_id: str,
    topic_id: int,
    *,
    unique_id: str = "",
    company_name: str = "",
    ticket_name: str = "",
    priority: str = "",
    status: str = "",
    owner_id: str = "",
    owner_name: str = "",
    topic_state: str = "active",
    delete_after_at: Optional[str] = None,
    last_client_reply_at: Optional[str] = None,
    last_staff_reply_at: Optional[str] = None,
    pre_sla_notify_at: Optional[str] = None,
    pre_sla_sent_at: Optional[str] = None,
    hde_link: str = "",
    deleted_at: Optional[str] = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ticket_topics (
                ticket_id,
                unique_id,
                topic_id,
                company_name,
                ticket_name,
                priority,
                status,
                owner_id,
                owner_name,
                topic_state,
                delete_after_at,
                last_client_reply_at,
                last_staff_reply_at,
                pre_sla_notify_at,
                pre_sla_sent_at,
                hde_link,
                deleted_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(ticket_id) DO UPDATE SET
                unique_id = excluded.unique_id,
                topic_id = excluded.topic_id,
                company_name = excluded.company_name,
                ticket_name = excluded.ticket_name,
                priority = excluded.priority,
                status = excluded.status,
                owner_id = excluded.owner_id,
                owner_name = excluded.owner_name,
                topic_state = excluded.topic_state,
                delete_after_at = excluded.delete_after_at,
                last_client_reply_at = COALESCE(excluded.last_client_reply_at, ticket_topics.last_client_reply_at),
                last_staff_reply_at = COALESCE(excluded.last_staff_reply_at, ticket_topics.last_staff_reply_at),
                pre_sla_notify_at = excluded.pre_sla_notify_at,
                pre_sla_sent_at = excluded.pre_sla_sent_at,
                hde_link = excluded.hde_link,
                deleted_at = excluded.deleted_at,
                updated_at = datetime('now')
            """,
            (
                ticket_id,
                unique_id or ticket_id,
                topic_id,
                company_name,
                ticket_name,
                priority,
                status,
                owner_id,
                owner_name,
                topic_state,
                delete_after_at,
                last_client_reply_at,
                last_staff_reply_at,
                pre_sla_notify_at,
                pre_sla_sent_at,
                hde_link,
                deleted_at,
            ),
        )
        await db.commit()


async def update_topic(ticket_id: str, **fields: Any) -> None:
    if not fields:
        return

    invalid = set(fields) - UPDATABLE_FIELDS
    if invalid:
        raise ValueError(f"Unsupported topic fields: {sorted(invalid)}")

    assignments = [f"{field} = ?" for field in fields]
    values = list(fields.values())
    assignments.append("updated_at = datetime('now')")
    values.append(ticket_id)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"""
            UPDATE ticket_topics
            SET {", ".join(assignments)}
            WHERE ticket_id = ?
            """,
            values,
        )
        await db.commit()


async def set_topic_pending_delete(ticket_id: str, delete_after_at: str) -> None:
    await update_topic(
        ticket_id,
        topic_state="pending_delete",
        delete_after_at=delete_after_at,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
    )


async def set_topic_active(ticket_id: str) -> None:
    await update_topic(
        ticket_id,
        topic_state="active",
        delete_after_at=None,
        deleted_at=None,
    )


async def mark_topic_deleted(ticket_id: str) -> None:
    record = await get_topic(ticket_id)
    await update_topic(
        ticket_id,
        topic_state="deleted",
        delete_after_at=None,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        deleted_at=to_storage(utcnow()),
    )
    if record is not None:
        await delete_reply_draft(record.topic_id)
        await delete_topic_media_cache(record.topic_id)


async def schedule_pre_sla(ticket_id: str, notify_at: str) -> None:
    await update_topic(
        ticket_id,
        pre_sla_notify_at=notify_at,
        pre_sla_sent_at=None,
    )


async def clear_pre_sla(ticket_id: str) -> None:
    await update_topic(
        ticket_id,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
    )


async def mark_pre_sla_sent(ticket_id: str, sent_at: Optional[str] = None) -> None:
    await update_topic(
        ticket_id,
        pre_sla_sent_at=sent_at or to_storage(utcnow()),
    )


async def list_due_pre_sla(now_value: str) -> list[TicketTopic]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM ticket_topics
            WHERE topic_state = 'active'
              AND pre_sla_notify_at IS NOT NULL
              AND pre_sla_sent_at IS NULL
              AND pre_sla_notify_at <= ?
            ORDER BY pre_sla_notify_at ASC
            """,
            (now_value,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def list_due_deletions(now_value: str) -> list[TicketTopic]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM ticket_topics
            WHERE topic_state = 'pending_delete'
              AND delete_after_at IS NOT NULL
              AND delete_after_at <= ?
            ORDER BY delete_after_at ASC
            """,
            (now_value,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def count_active_topics() -> int:
    return await _count_topics("topic_state = 'active'")


async def count_pending_delete_topics() -> int:
    return await _count_topics("topic_state = 'pending_delete'")


async def count_pending_pre_sla_topics() -> int:
    return await _count_topics(
        "topic_state = 'active' AND pre_sla_notify_at IS NOT NULL AND pre_sla_sent_at IS NULL"
    )


async def count_total_topics() -> int:
    return await _count_topics("1 = 1")


async def _count_topics(where_clause: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM ticket_topics WHERE {where_clause}"
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


async def save_processed_event(event_key: str, event_type: str, ticket_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO processed_events (event_key, event_type, ticket_id)
            VALUES (?, ?, ?)
            """,
            (event_key, event_type, ticket_id),
        )
        await db.commit()
    return cursor.rowcount > 0


async def was_processed(event_key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM processed_events WHERE event_key = ?",
            (event_key,),
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None


def _row_to_draft(row: aiosqlite.Row) -> ReplyDraft:
    return ReplyDraft(
        topic_id=row["topic_id"],
        ticket_id=row["ticket_id"],
        text=row["text"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_cached_topic_media(row: aiosqlite.Row) -> CachedTopicMedia:
    return CachedTopicMedia(
        topic_id=row["topic_id"],
        message_id=row["message_id"],
        media_group_id=row["media_group_id"],
        attachment_kind=row["attachment_kind"],
        file_id=row["file_id"],
        filename=row["filename"] or "",
        content_type=row["content_type"] or "",
        text=row["text"] or "",
        created_at=row["created_at"],
    )


async def save_reply_draft(topic_id: int, ticket_id: str, text: str, created_by: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO reply_drafts (topic_id, ticket_id, text, created_by, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(topic_id) DO UPDATE SET
                ticket_id = excluded.ticket_id,
                text = excluded.text,
                created_by = excluded.created_by,
                updated_at = datetime('now')
            """,
            (topic_id, ticket_id, text, created_by),
        )
        await db.commit()


async def get_reply_draft(topic_id: int) -> Optional[ReplyDraft]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reply_drafts WHERE topic_id = ?",
            (topic_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_draft(row) if row else None


async def delete_reply_draft(topic_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM reply_drafts WHERE topic_id = ?",
            (topic_id,),
        )
        await db.commit()


async def cache_topic_media(
    *,
    topic_id: int,
    message_id: int,
    media_group_id: Optional[str],
    attachment_kind: str,
    file_id: str,
    filename: str = "",
    content_type: str = "",
    text: str = "",
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO topic_media_cache (
                topic_id,
                message_id,
                media_group_id,
                attachment_kind,
                file_id,
                filename,
                content_type,
                text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(topic_id, message_id) DO UPDATE SET
                media_group_id = excluded.media_group_id,
                attachment_kind = excluded.attachment_kind,
                file_id = excluded.file_id,
                filename = excluded.filename,
                content_type = excluded.content_type,
                text = excluded.text
            """,
            (
                topic_id,
                message_id,
                media_group_id,
                attachment_kind,
                file_id,
                filename,
                content_type,
                text,
            ),
        )
        await db.commit()


async def list_cached_topic_media_group(topic_id: int, media_group_id: str) -> list[CachedTopicMedia]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM topic_media_cache
            WHERE topic_id = ? AND media_group_id = ?
            ORDER BY message_id ASC
            """,
            (topic_id, media_group_id),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_cached_topic_media(row) for row in rows]


async def delete_topic_media_cache(topic_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM topic_media_cache WHERE topic_id = ?",
            (topic_id,),
        )
        await db.commit()


async def save_sent_message(
    telegram_message_id: int,
    topic_id: int,
    ticket_id: str,
    hde_entity_id: int,
    entity_type: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO sent_hde_messages
                (telegram_message_id, topic_id, ticket_id, hde_entity_id, entity_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_message_id, topic_id, ticket_id, hde_entity_id, entity_type),
        )
        await db.commit()


async def get_sent_message(
    telegram_message_id: int,
    topic_id: int,
) -> Optional[SentHdeMessage]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM sent_hde_messages
            WHERE telegram_message_id = ? AND topic_id = ?
            """,
            (telegram_message_id, topic_id),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return SentHdeMessage(
        telegram_message_id=row["telegram_message_id"],
        topic_id=row["topic_id"],
        ticket_id=row["ticket_id"],
        hde_entity_id=row["hde_entity_id"],
        entity_type=row["entity_type"],
        created_at=row["created_at"],
    )


async def delete_sent_message(telegram_message_id: int, topic_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM sent_hde_messages WHERE telegram_message_id = ? AND topic_id = ?",
            (telegram_message_id, topic_id),
        )
        await db.commit()


async def list_overnight_assigned(night_start: str, night_end: str) -> list[TicketTopic]:
    """Return topics where last_assigned_at is between night_start and night_end (UTC strings)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM ticket_topics
            WHERE last_assigned_at >= ?
              AND last_assigned_at < ?
              AND topic_state != 'deleted'
            ORDER BY last_assigned_at ASC
            """,
            (night_start, night_end),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def list_active_topics() -> list[TicketTopic]:
    """Return all topics with topic_state = 'active'."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_topics WHERE topic_state = 'active' ORDER BY created_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def list_topics_by_state(state: str) -> list[TicketTopic]:
    """Return all topics with the given topic_state."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_topics WHERE topic_state = ? ORDER BY created_at ASC",
            (state,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def save_general_message(ticket_id: str, message_id: int, ticket_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO unassigned_general_messages (ticket_id, message_id, ticket_name)
            VALUES (?, ?, ?)
            ON CONFLICT(ticket_id) DO UPDATE SET
                message_id = excluded.message_id,
                ticket_name = excluded.ticket_name
            """,
            (ticket_id, message_id, ticket_name),
        )
        await db.commit()


async def get_general_message(ticket_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ticket_id, message_id, ticket_name, created_at FROM unassigned_general_messages WHERE ticket_id = ?",
            (ticket_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def delete_general_message(ticket_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM unassigned_general_messages WHERE ticket_id = ?",
            (ticket_id,),
        )
        await db.commit()


async def count_general_messages() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM unassigned_general_messages"
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


async def is_report_sent(report_date: "date") -> bool:
    """Return True if the daily report was already sent for *report_date*."""
    key = report_date.strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM report_runs WHERE report_date = ?", (key,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_report_sent(report_date: "date") -> None:
    """Record that the daily report was sent for *report_date*."""
    key = report_date.strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO report_runs (report_date) VALUES (?)", (key,)
        )
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    """Return a value from bot_settings, or *default* if not set."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    """Persist a key-value pair in bot_settings."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings(key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Knowledge items
# ---------------------------------------------------------------------------

@dataclass
class KnowledgeItem:
    id: int
    source: str
    ticket_id: Optional[str]
    title: Optional[str]
    content: str
    quality: str


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
    async with aiosqlite.connect(DB_PATH) as db:
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


async def update_knowledge_embedding(item_id: int, embedding: bytes) -> None:
    """Store the embedding blob for an existing knowledge item."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE knowledge_items SET embedding = ? WHERE id = ?",
            (embedding, item_id),
        )
        await db.commit()


async def list_knowledge_items_without_embedding() -> list[tuple[int, str]]:
    """Return (id, content) for rows missing an embedding."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, content FROM knowledge_items WHERE embedding IS NULL AND quality NOT IN ('bad', 'expired')"
        ) as cur:
            return await cur.fetchall()


async def list_all_knowledge_embeddings() -> list[tuple[int, str, bytes, str]]:
    """Return (id, content, embedding, company_id) for all indexed items."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, content, embedding, COALESCE(company_id, '') FROM knowledge_items "
            "WHERE embedding IS NOT NULL AND quality NOT IN ('bad', 'expired')"
        ) as cur:
            return await cur.fetchall()


async def count_knowledge_by_source() -> dict[str, int]:
    """Return {source: count} statistics."""
    async with aiosqlite.connect(DB_PATH) as db:
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
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO ai_feedback_pending
                (topic_id, ticket_id, history, title, answer_text, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (topic_id, ticket_id, history, title, answer_text, expires_at),
        )
        await db.commit()


async def get_ai_feedback_pending(topic_id: int) -> Optional[dict]:
    """Return pending correction state or None if expired/missing."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ticket_id, history, title, answer_text, expires_at "
            "FROM ai_feedback_pending WHERE topic_id = ?",
            (topic_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    from datetime import datetime, timezone
    expires = datetime.fromisoformat(row[4])
    if datetime.now(timezone.utc) > expires:
        await delete_ai_feedback_pending(topic_id)
        return None
    return {"ticket_id": row[0], "history": row[1], "title": row[2], "answer_text": row[3]}


async def delete_ai_feedback_pending(topic_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM ai_feedback_pending WHERE topic_id = ?", (topic_id,)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# AI status helpers
# ---------------------------------------------------------------------------

async def count_items_without_embedding() -> int:
    """Count knowledge_items that have no embedding blob (failed or pending)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM knowledge_items "
            "WHERE embedding IS NULL AND quality NOT IN ('bad', 'expired')"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_last_knowledge_item_date() -> str | None:
    """Return ISO timestamp of the most recently created knowledge_item, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT MAX(created_at) FROM knowledge_items"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None


async def list_items_without_company() -> list[tuple[int, str]]:
    """Return (id, ticket_id) for hde_closed items missing company_name."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ticket_id FROM knowledge_items "
            "WHERE source = 'hde_closed' "
            "AND (company_name IS NULL OR company_name = '') "
            "AND ticket_id IS NOT NULL"
        ) as cur:
            return await cur.fetchall()


async def update_knowledge_company(item_id: int, company_id: str, company_name: str) -> None:
    """Set company_id and company_name for an existing knowledge item."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE knowledge_items SET company_id = ?, company_name = ? WHERE id = ?",
            (company_id or None, company_name or None, item_id),
        )
        await db.commit()


async def fts_search_knowledge(query: str, limit: int = 10) -> list[tuple[int, str]]:
    """BM25 full-text search via FTS5. Returns (id, content) ordered by relevance."""
    import re
    # Strip FTS5 special characters to avoid syntax errors
    clean = re.sub(r'["\(\)\^\*\-]', ' ', query).strip()
    if not clean:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            async with db.execute(
                "SELECT rowid, content FROM knowledge_fts WHERE content MATCH ? ORDER BY rank LIMIT ?",
                (clean, limit),
            ) as cur:
                return [(int(row[0]), row[1]) for row in await cur.fetchall()]
        except Exception:
            return []


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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE solution_patterns SET use_count = use_count + 1 WHERE id = ?",
            (pattern_id,),
        )
        await db.commit()


async def list_solution_patterns(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM solution_patterns ORDER BY use_count DESC, created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def pattern_exists_similar(
    equipment: Optional[str],
    problem_type: str,
) -> bool:
    """Return True if a pattern with same equipment and similar problem_type exists."""
    patterns = await list_solution_patterns(limit=200)
    for p in patterns:
        if p["equipment"] != equipment:
            continue
        ratio = difflib.SequenceMatcher(
            None, p["problem_type"].lower(), problem_type.lower()
        ).ratio()
        if ratio >= 0.7:
            return True
    return False


async def count_solution_patterns_by_equipment() -> dict[str, int]:
    """Return {equipment_label: count} for reporting."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(equipment, 'Без бренда'), COUNT(*) "
            "FROM solution_patterns GROUP BY equipment ORDER BY COUNT(*) DESC"
        ) as cur:
            return dict(await cur.fetchall())


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
    company_id: str = "",
    company_name: str = "",
) -> tuple[int, bool]:
    """Insert or update knowledge item by ticket_id+source. Returns (id, was_created).

    If ticket_id is provided and a row with same (ticket_id, source) exists:
    -> UPDATE content, title, reset embedding=NULL (triggers re-embedding), update FTS.
    Otherwise -> INSERT new row.
    """
    async with aiosqlite.connect(DB_PATH) as db:
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
                    "SET content=?, title=?, quality=?, url=?, "
                    "company_id=?, company_name=?, embedding=NULL, "
                    "created_at=datetime('now') "
                    "WHERE id=?",
                    (content, title or None, quality, url or None,
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
            "(source, ticket_id, title, content, quality, url, company_id, company_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, ticket_id or None, title or None, content, quality,
             url or None, company_id or None, company_name or None),
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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


async def update_knowledge_last_used(item_ids: list[int]) -> None:
    """Update last_used_at for items where it's NULL or older than 24 hours (throttled)."""
    if not item_ids:
        return
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    placeholders = ",".join("?" * len(item_ids))
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT content FROM prompt_versions WHERE status='active' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return row["content"] if row else None


async def save_prompt_version(content: str, score: float | None, proposed_by: str) -> int:
    """Save a candidate prompt version. Returns its id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO prompt_versions (content, score, proposed_by, status) VALUES (?,?,?,?)",
            (content, score, proposed_by, "candidate"),
        )
        await db.commit()
        return cursor.lastrowid


async def apply_prompt_version(version_id: int) -> None:
    """Mark version as active, all others as rejected."""
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE prompt_versions SET status='rejected' WHERE status='candidate'"
        )
        await db.commit()
