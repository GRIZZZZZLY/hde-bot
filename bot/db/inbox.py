"""Durable webhook inbox (ADR 2026-07-12).

Событие обработано только после статуса 'completed'; ответ 200 OK означает
«событие надёжно сохранено» (pending), а не «обработка завершена». Единственная
система дедупликации приёма вебхуков (инвариант I7): дедуп по UNIQUE event_id.

Модель попыток: attempts инкрементится при claim (каждый claim = одна попытка
обработки). Краш процесса оставляет строку в 'processing'; после истечения lease
её переклеймит следующий проход (в т.ч. после рестарта). Исчерпание attempts →
'dead'. Рассчитано на один воркер в процессе (SQLite — один писатель)."""
from __future__ import annotations

import datetime as _dt
import json

import aiosqlite

from .core import connect

_MAX_ATTEMPTS = 5
_LEASE_SECONDS = 120
_BACKOFF_BASE_SECONDS = 30
_TERMINAL = ("completed", "dead")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(ts: _dt.datetime) -> str:
    # без tz-суффикса: все метки в UTC, ISO лексикографически сортируется
    return ts.strftime("%Y-%m-%dT%H:%M:%S")


def event_ticket_id(payload: str) -> str:
    """ticket_id события; '' для битого payload (его обработка сама упадёт в failed)."""
    try:
        return str(json.loads(payload).get("ticket_id") or "")
    except (ValueError, AttributeError):
        return ""


async def enqueue_event(event_id: str, payload: str, *, now: _dt.datetime | None = None) -> bool:
    """Durable-сохранение события. True — новое, False — дубль доставки (I7-дедуп)."""
    now = now or _utcnow()
    async with connect() as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO webhook_inbox (event_id, payload, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (event_id, payload, _iso(now)),
        )
        await db.commit()
        return cur.rowcount > 0


async def claim_next_event(
    *,
    lease_seconds: int = _LEASE_SECONDS,
    max_attempts: int = _MAX_ATTEMPTS,
    now: _dt.datetime | None = None,
    busy_tickets: frozenset[str] = frozenset(),
) -> dict | None:
    """Атомарно взять следующее обрабатываемое событие: pending со сроком ИЛИ
    processing с истёкшим lease (краш-реклейм). Исчерпавшие attempts → 'dead' и
    пропускаются. Возвращает строку с уже инкрементированным attempts или None.

    События тикетов из busy_tickets (уже в работе у воркера) пропускаются: так
    события одного тикета идут строго по очереди, а долгий хендлер (дольше lease)
    не переклеймится, пока ещё работает."""
    now = now or _utcnow()
    now_s = _iso(now)
    lease_s = _iso(now + _dt.timedelta(seconds=lease_seconds))
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM webhook_inbox WHERE "
            "(status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
            "OR (status='processing' AND (lease_until IS NULL OR lease_until <= ?)) "
            "ORDER BY id",
            (now_s, now_s),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            if int(row["attempts"]) >= max_attempts:
                await db.execute(
                    "UPDATE webhook_inbox SET status='dead', lease_until=NULL WHERE event_id=?",
                    (row["event_id"],),
                )
                await db.commit()
                continue  # проверить следующего кандидата
            if busy_tickets and event_ticket_id(row["payload"]) in busy_tickets:
                continue
            attempts = int(row["attempts"]) + 1
            await db.execute(
                "UPDATE webhook_inbox SET status='processing', attempts=?, lease_until=? "
                "WHERE event_id=?",
                (attempts, lease_s, row["event_id"]),
            )
            await db.commit()
            result = dict(row)
            result["attempts"] = attempts
            result["status"] = "processing"
            return result
        return None


async def mark_completed(event_id: str) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE webhook_inbox SET status='completed', lease_until=NULL WHERE event_id=?",
            (event_id,),
        )
        await db.commit()


async def mark_failed(
    event_id: str,
    error: str,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
    backoff_seconds: int = _BACKOFF_BASE_SECONDS,
    now: _dt.datetime | None = None,
) -> str:
    """Пометить попытку неуспешной. attempts уже инкрементирован на claim; здесь —
    backoff (pending + next_attempt_at) или 'dead', если attempts исчерпаны."""
    now = now or _utcnow()
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT attempts FROM webhook_inbox WHERE event_id=?", (event_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return "missing"
        attempts = int(row["attempts"])
        if attempts >= max_attempts:
            await db.execute(
                "UPDATE webhook_inbox SET status='dead', last_error=?, lease_until=NULL "
                "WHERE event_id=?",
                (str(error)[:500], event_id),
            )
            status = "dead"
        else:
            backoff = backoff_seconds * (2 ** max(attempts - 1, 0))
            nxt = _iso(now + _dt.timedelta(seconds=backoff))
            await db.execute(
                "UPDATE webhook_inbox SET status='pending', last_error=?, "
                "next_attempt_at=?, lease_until=NULL WHERE event_id=?",
                (str(error)[:500], nxt, event_id),
            )
            status = "pending"
        await db.commit()
        return status


async def get_inbox_event(event_id: str) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM webhook_inbox WHERE event_id=?", (event_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def count_inbox_by_status() -> dict:
    async with connect() as db:
        async with db.execute(
            "SELECT status, COUNT(*) FROM webhook_inbox GROUP BY status"
        ) as cur:
            rows = await cur.fetchall()
    return {r[0]: int(r[1]) for r in rows}
