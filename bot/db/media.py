from __future__ import annotations

from typing import Optional

import aiosqlite

from .core import (
    CachedTopicMedia,
    ReplyDraft,
    SentHdeMessage,
    _row_to_cached_topic_media,
    _row_to_draft,
    db_path,
)


async def save_reply_draft(topic_id: int, ticket_id: str, text: str, created_by: int) -> None:
    async with aiosqlite.connect(db_path()) as db:
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
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reply_drafts WHERE topic_id = ?",
            (topic_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_draft(row) if row else None


async def delete_reply_draft(topic_id: int) -> None:
    async with aiosqlite.connect(db_path()) as db:
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
    async with aiosqlite.connect(db_path()) as db:
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
    async with aiosqlite.connect(db_path()) as db:
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
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(
            "DELETE FROM topic_media_cache WHERE topic_id = ?",
            (topic_id,),
        )
        await db.commit()


async def gc_stale_media_cache(hours: int = 1) -> int:
    """Delete topic_media_cache rows older than N hours. Returns deleted row count.

    Media groups arrive within seconds — rows older than an hour are guaranteed
    irrelevant for reply-stitching and otherwise accumulate until topic deletion.
    """
    async with aiosqlite.connect(db_path()) as db:
        cursor = await db.execute(
            "DELETE FROM topic_media_cache WHERE created_at < datetime('now', ?)",
            (f"-{int(hours)} hours",),
        )
        deleted = cursor.rowcount or 0
        await db.commit()
    return deleted


async def save_sent_message(
    telegram_message_id: int,
    topic_id: int,
    ticket_id: str,
    hde_entity_id: int,
    entity_type: str,
) -> None:
    async with aiosqlite.connect(db_path()) as db:
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
    async with aiosqlite.connect(db_path()) as db:
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
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(
            "DELETE FROM sent_hde_messages WHERE telegram_message_id = ? AND topic_id = ?",
            (telegram_message_id, topic_id),
        )
        await db.commit()
