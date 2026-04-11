from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import aiosqlite

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
                created_at   TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_feedback_pending (
                topic_id   INTEGER PRIMARY KEY,
                ticket_id  TEXT NOT NULL,
                history    TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL
            )
            """
        )
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
) -> int:
    """Insert a new knowledge item. Returns the new row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO knowledge_items
                (source, ticket_id, title, content, embedding, quality, url, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, ticket_id or None, title or None, content,
             embedding, quality, url or None, content_hash or None),
        )
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
            "SELECT id, content FROM knowledge_items WHERE embedding IS NULL AND quality != 'bad'"
        ) as cur:
            return await cur.fetchall()


async def list_all_knowledge_embeddings() -> list[tuple[int, str, bytes]]:
    """Return (id, content, embedding) for all indexed items."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, content, embedding FROM knowledge_items "
            "WHERE embedding IS NOT NULL AND quality != 'bad'"
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
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO ai_feedback_pending
                (topic_id, ticket_id, history, title, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (topic_id, ticket_id, history, title, expires_at),
        )
        await db.commit()


async def get_ai_feedback_pending(topic_id: int) -> Optional[dict]:
    """Return pending correction state or None if expired/missing."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ticket_id, history, title, expires_at "
            "FROM ai_feedback_pending WHERE topic_id = ?",
            (topic_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    from datetime import datetime, timezone
    expires = datetime.fromisoformat(row[3])
    if datetime.now(timezone.utc) > expires:
        await delete_ai_feedback_pending(topic_id)
        return None
    return {"ticket_id": row[0], "history": row[1], "title": row[2]}


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
            "WHERE embedding IS NULL AND quality != 'bad'"
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
