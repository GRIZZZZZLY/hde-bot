from __future__ import annotations

from typing import Optional

import aiosqlite

from .core import db_path


async def save_general_message(ticket_id: str, message_id: int, ticket_name: str) -> None:
    async with aiosqlite.connect(db_path()) as db:
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
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ticket_id, message_id, ticket_name, created_at FROM unassigned_general_messages WHERE ticket_id = ?",
            (ticket_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def delete_general_message(ticket_id: str) -> None:
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(
            "DELETE FROM unassigned_general_messages WHERE ticket_id = ?",
            (ticket_id,),
        )
        await db.commit()


async def count_general_messages() -> int:
    async with aiosqlite.connect(db_path()) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM unassigned_general_messages"
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


async def list_general_messages() -> list[dict]:
    """Return all currently posted General notifications."""
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ticket_id, message_id, ticket_name FROM unassigned_general_messages"
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Pending General notifications (tickets that arrived outside work hours)
# ---------------------------------------------------------------------------

async def _ensure_pending_general_table(db_conn) -> None:
    await db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_general_tickets (
            ticket_id   TEXT PRIMARY KEY,
            display_id  TEXT DEFAULT '',
            ticket_name TEXT DEFAULT '',
            link        TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        )
        """
    )


async def save_pending_general(
    ticket_id: str, display_id: str, ticket_name: str, link: str
) -> None:
    """Queue an unassigned ticket for General notification at work-start flush."""
    async with aiosqlite.connect(db_path()) as db:
        await _ensure_pending_general_table(db)
        await db.execute(
            """
            INSERT OR REPLACE INTO pending_general_tickets
                (ticket_id, display_id, ticket_name, link)
            VALUES (?, ?, ?, ?)
            """,
            (ticket_id, display_id, ticket_name, link),
        )
        await db.commit()


async def list_pending_general() -> list[dict]:
    """Return all pending General notifications ordered by created_at."""
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_pending_general_table(db)
        async with db.execute(
            "SELECT ticket_id, display_id, ticket_name, link FROM pending_general_tickets ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_pending_general(ticket_id: str) -> None:
    async with aiosqlite.connect(db_path()) as db:
        await _ensure_pending_general_table(db)
        await db.execute(
            "DELETE FROM pending_general_tickets WHERE ticket_id = ?",
            (ticket_id,),
        )
        await db.commit()


async def is_report_sent(report_date: "date") -> bool:
    """Return True if the daily report was already sent for *report_date*."""
    key = report_date.strftime("%Y-%m-%d")
    async with aiosqlite.connect(db_path()) as db:
        async with db.execute(
            "SELECT 1 FROM report_runs WHERE report_date = ?", (key,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_report_sent(report_date: "date") -> bool:
    """Atomically claim *report_date* for the daily report.

    Returns True if this caller claimed it (the row was inserted), False if it
    was already claimed. Used as a claim-before-run guard so two triggers in the
    same window (two bot instances, or a restart mid-run) never write twice.
    """
    key = report_date.strftime("%Y-%m-%d")
    async with aiosqlite.connect(db_path()) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO report_runs (report_date) VALUES (?)", (key,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def clear_report_sent(report_date: "date") -> None:
    """Release a claim made by mark_report_sent (e.g. when the run failed)."""
    key = report_date.strftime("%Y-%m-%d")
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(
            "DELETE FROM report_runs WHERE report_date = ?", (key,)
        )
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    """Return a value from bot_settings, or *default* if not set."""
    async with aiosqlite.connect(db_path()) as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    """Persist a key-value pair in bot_settings."""
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings(key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()
