from __future__ import annotations

from typing import Any, Optional

import aiosqlite

from ..time_utils import to_storage, utcnow
from .core import TicketTopic, UPDATABLE_FIELDS, _row_to_topic, connect
from .media import delete_reply_draft, delete_topic_media_cache


async def get_topic(ticket_id: str) -> Optional[TicketTopic]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_topics WHERE ticket_id = ?",
            (ticket_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_topic(row) if row else None


async def get_topic_by_topic_id(topic_id: int) -> Optional[TicketTopic]:
    async with connect() as db:
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
    async with connect() as db:
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

    async with connect() as db:
        await db.execute(
            f"""
            UPDATE ticket_topics
            SET {", ".join(assignments)}
            WHERE ticket_id = ?
            """,
            values,
        )
        await db.commit()


async def get_common_env_for_company(
    company_name: str, exclude_ticket_id: str = "", limit: int = 10
) -> Optional[str]:
    """Самое частое непустое env_option_id среди последних топиков компании.

    Используется как слабый приор для классификатора «Окружение».
    None, если компании нет или ни одного определённого окружения.
    """
    if not company_name.strip():
        return None
    async with connect() as db:
        async with db.execute(
            """
            SELECT env_option_id, COUNT(*) AS cnt FROM (
                SELECT env_option_id FROM ticket_topics
                WHERE company_name = ? AND ticket_id != ?
                  AND env_option_id IS NOT NULL AND env_option_id != ''
                ORDER BY updated_at DESC LIMIT ?
            ) GROUP BY env_option_id ORDER BY cnt DESC LIMIT 1
            """,
            (company_name, exclude_ticket_id, limit),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def append_photo_descriptions(ticket_id: str, descriptions: list[str]) -> None:
    """Append Vision-generated photo descriptions to the topic (newline-joined).

    Used by knowledge indexing to include image content in embeddings.
    No-op if descriptions is empty or ticket is unknown.
    """
    if not descriptions:
        return
    addition = "\n".join(d.strip() for d in descriptions if d and d.strip())
    if not addition:
        return
    async with connect() as db:
        await db.execute(
            """
            UPDATE ticket_topics
            SET photo_descriptions = TRIM(COALESCE(photo_descriptions, '') || CHAR(10) || ?, CHAR(10)),
                updated_at = datetime('now')
            WHERE ticket_id = ?
            """,
            (addition, ticket_id),
        )
        await db.commit()


async def set_topic_pending_delete(ticket_id: str, delete_after_at: str) -> None:
    await update_topic(
        ticket_id,
        topic_state="pending_delete",
        delete_after_at=delete_after_at,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
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
        pre_sla_message_id=None,
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
        pre_sla_message_id=None,
    )


async def clear_pre_sla(ticket_id: str) -> None:
    await update_topic(
        ticket_id,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
        reassurance_sent_at=None,
    )


async def mark_pre_sla_sent(ticket_id: str, message_id: int, sent_at: Optional[str] = None) -> None:
    await update_topic(
        ticket_id,
        pre_sla_sent_at=sent_at or to_storage(utcnow()),
        pre_sla_message_id=message_id,
    )


async def list_due_pre_sla(now_value: str) -> list[TicketTopic]:
    async with connect() as db:
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


async def list_active_pre_sla() -> list[TicketTopic]:
    """Тикеты, у которых pre-SLA уже отправлен и ждёт обновления счётчика."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM ticket_topics
            WHERE topic_state = 'active'
              AND pre_sla_notify_at IS NOT NULL
              AND pre_sla_sent_at IS NOT NULL
              AND pre_sla_message_id IS NOT NULL
            ORDER BY pre_sla_notify_at ASC
            """,
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def list_topics_missing_summary() -> list[TicketTopic]:
    """Активные топики где клиент писал, но AI саммари ещё не отправлялось."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM ticket_topics
            WHERE topic_state = 'active'
              AND last_client_reply_at IS NOT NULL
              AND ai_summary_sent_at IS NULL
            ORDER BY last_client_reply_at ASC
            """,
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def list_due_deletions(now_value: str) -> list[TicketTopic]:
    async with connect() as db:
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
    async with connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM ticket_topics WHERE {where_clause}"
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


# save_processed_event / delete_processed_event удалены: приём вебхуков дедупится
# durable inbox (webhook_inbox), см. ADR 2026-07-12 (инвариант I7 — один дедуп).


async def was_processed(event_key: str) -> bool:
    async with connect() as db:
        async with db.execute(
            "SELECT 1 FROM processed_events WHERE event_key = ?",
            (event_key,),
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None


async def list_overnight_assigned(night_start: str, night_end: str) -> list[TicketTopic]:
    """Return topics where last_assigned_at is between night_start and night_end (UTC strings)."""
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_topics WHERE topic_state = 'active' ORDER BY created_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]


async def list_topics_by_state(state: str) -> list[TicketTopic]:
    """Return all topics with the given topic_state."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_topics WHERE topic_state = ? ORDER BY created_at ASC",
            (state,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_topic(row) for row in rows]
