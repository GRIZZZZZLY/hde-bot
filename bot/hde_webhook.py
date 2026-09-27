from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Callable

from aiohttp import web
from aiogram import Bot

from . import db
from . import general_channel
from . import metrics
from .client_media import extract_client_attachment_refs
from .config import config
from .topic_manager import (
    handle_assigned_on_create,
    handle_client_reply,
    handle_owner_changed,
    handle_staff_reply,
    handle_ticket_closed,
    handle_ticket_updated,
)

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Callable[[Bot, dict], object]] = {
    "assigned_on_create": handle_assigned_on_create,
    "owner_changed": handle_owner_changed,
    "ticket_updated": handle_ticket_updated,
    "client_reply": handle_client_reply,
    "staff_reply": handle_staff_reply,
    "ticket_closed": handle_ticket_closed,
}

REQUIRED_FIELDS = {
    "assigned_on_create": ["ticket_id"],
    "owner_changed": ["ticket_id"],
    "ticket_updated": ["ticket_id"],
    "client_reply": ["ticket_id"],
    "staff_reply": ["ticket_id"],
    "ticket_closed": ["ticket_id"],
}


def _normalize_payload(payload: dict) -> dict:
    event_type = str(payload.get("event_type") or "").strip()
    ticket_id = str(payload.get("ticket_id") or payload.get("unique_id") or "").strip()
    unique_id = str(payload.get("unique_id") or ticket_id).strip()

    return {
        "event_type": event_type,
        "ticket_id": ticket_id,
        "unique_id": unique_id,
        "ticket_name": str(payload.get("ticket_name") or "").strip(),
        "company_name": str(payload.get("company_name") or "").strip(),
        "priority": str(payload.get("priority") or "").strip(),
        "status": str(payload.get("status") or "").strip(),
        "owner_id": str(payload.get("owner_id") or "").strip(),
        "owner_name": str(payload.get("owner_name") or "").strip(),
        "user_name": str(payload.get("user_name") or "").strip(),
        "message": str(
            payload.get("message")
            or payload.get("answer_last_without_html")
            or payload.get("answer_last")
            or ""
        ).strip(),
        "link": str(payload.get("link") or payload.get("link_staff") or "").strip(),
        "department": str(payload.get("department") or "").strip(),
        "date_update": str(payload.get("date_update") or "").strip(),
        "last_post_date": str(payload.get("last_post_date") or "").strip(),
        "sla_remaining_minutes": payload.get("sla_remaining_minutes", payload.get("sla_remaining")),
        "attachments": extract_client_attachment_refs(payload),
        "event_key": str(payload.get("event_key") or "").strip(),
    }


def _build_event_key(payload: dict) -> str:
    # Use stable key based on ticket_id + event_type + date_update so that
    # HDE retries / duplicate rule triggers for the same update are deduped.
    date_update = payload.get("date_update", "")
    if date_update:
        source = f"{payload['ticket_id']}:{payload['event_type']}:{date_update}"
    else:
        source = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def hde_webhook_handler(request: web.Request) -> web.Response:
    metrics.inc("webhook_received")
    try:
        raw_payload = await request.json()
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Invalid JSON in HDE webhook: %s", exc)
        return web.Response(status=400, text="Invalid JSON")

    if not config.hde_webhook_secret:
        # Fail closed: without a configured secret the endpoint would be open.
        logger.error("HDE_WEBHOOK_SECRET is not configured — rejecting webhook")
        return web.Response(status=403, text="Forbidden")
    secret = str(raw_payload.get("secret") or "")
    if secret != config.hde_webhook_secret:
        logger.warning("Invalid webhook secret from %s", request.remote)
        return web.Response(status=403, text="Forbidden")

    payload = _normalize_payload(raw_payload)
    event_type = payload["event_type"]
    if not event_type:
        logger.warning("Missing event_type in HDE webhook payload")
        return web.Response(status=400, text="Missing event_type")

    required_fields = REQUIRED_FIELDS.get(event_type)
    if required_fields is None:
        logger.warning("Unknown event_type: %s", event_type)
        return web.Response(status=400, text=f"Unknown event_type: {event_type}")

    for field in required_fields:
        if not payload.get(field):
            logger.warning("Missing required field '%s' for event '%s'", field, event_type)
            return web.Response(status=400, text=f"Missing field: {field}")

    event_key = payload["event_key"] or _build_event_key(payload)
    # Durable enqueue ДО ACK (ADR I4/I5): 200 OK = событие надёжно сохранено
    # (pending), а не «обработано». Единственная система дедупа приёма (I7):
    # дубль доставки (тот же event_id) → 200 без повторной работы.
    is_new = await db.enqueue_event(event_key, json.dumps(payload, ensure_ascii=False))
    if not is_new:
        logger.info("Duplicate event %s for ticket %s", event_type, payload["ticket_id"])
        metrics.inc("webhook_duplicate")
        return web.Response(status=200, text="Duplicate OK")

    # Kick немедленной обработки. Durability гарантирует: если задача умрёт
    # (краш процесса), событие подхватит периодический воркер (bot.main).
    bot: Bot = request.app["bot"]
    task = asyncio.create_task(drain_inbox(bot))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return web.Response(status=200, text="OK")


# Strong refs so kick tasks aren't garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()

# Один дрен за раз: SQLite — один писатель, на процесс один воркер. Сериализует
# kick из вебхука и периодический воркер, чтобы не диспатчить событие дважды.
_drain_lock = asyncio.Lock()

# Сколько тикетов дрен ведёт одновременно. События одного тикета — строго по
# очереди, разные тикеты — параллельно: иначе история и черновик по одному
# тикету держат создание топиков остальных (прод 2026-08-30: 7 тикетов, взятых
# разом, последний топик через 313 с). Нагрузку на Groq и HDE ограничивают
# LLM_SEMAPHORE и лимитер HDE API, не этот предел; он лишь не даёт очереди,
# накопленной за простой, стартовать целиком.
_MAX_PARALLEL_TICKETS = 8


async def dispatch_event(bot: Bot, payload: dict) -> None:
    """Обработать одно событие: основной хендлер (fatal → исключение, воркер
    пометит failed и запланирует ретрай) + general-channel хуки (не-fatal)."""
    event_type = payload["event_type"]
    handler = HANDLERS[event_type]
    await handler(bot, payload)

    # General channel hooks — after the main handler, failures are non-fatal
    try:
        if event_type == "assigned_on_create":
            await general_channel.on_assigned_on_create(bot, payload)
        elif event_type == "owner_changed":
            await general_channel.on_owner_changed(bot, payload)
        elif event_type == "ticket_updated":
            await general_channel.on_ticket_updated(bot, payload)
        elif event_type == "ticket_closed":
            await general_channel.on_ticket_closed(bot, payload)
    except Exception as exc:
        logger.exception("General channel hook failed for event '%s': %s", event_type, exc)


async def drain_inbox(bot: Bot, *, max_events: int = 100, _dispatch_fn=None) -> dict:
    """Дренит durable inbox: claim → dispatch → completed/failed. Событие
    считается обработанным ТОЛЬКО после 'completed' (ADR I4). Исключение хендлера
    → mark_failed (ретрай с backoff, после лимита — dead)."""
    dispatch = _dispatch_fn or dispatch_event
    stats = {"processed": 0, "failed": 0}

    async def process(row) -> None:
        try:
            payload = json.loads(row["payload"])
            await dispatch(bot, payload)
        except Exception as exc:
            logger.exception("Inbox dispatch failed for %s: %s", row["event_id"], exc)
            metrics.inc("webhook_failed")
            await db.mark_failed(row["event_id"], str(exc))
            stats["failed"] += 1
            return
        await db.mark_completed(row["event_id"])
        metrics.inc("webhook_processed")
        stats["processed"] += 1
        logger.info("Processed %s for ticket %s",
                    payload.get("event_type"), payload.get("ticket_id"))

    running: dict[asyncio.Task, str] = {}   # задача → её ticket_id
    claimed = 0
    async with _drain_lock:
        try:
            while True:
                row = None
                if claimed < max_events and len(running) < _MAX_PARALLEL_TICKETS:
                    row = await db.claim_next_event(busy_tickets=frozenset(running.values()))
                if row is not None:
                    claimed += 1
                    running[asyncio.create_task(process(row))] = db.event_ticket_id(row["payload"])
                    continue
                if not running:
                    break
                # Ждём освобождения слота, но не дольше секунды: за это время мог
                # прийти вебхук по другому тикету — его надо взять сразу.
                done, _ = await asyncio.wait(
                    running, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    running.pop(task)
                    if not task.cancelled() and task.exception() is not None:
                        logger.error("Inbox bookkeeping failed: %s", task.exception())
        finally:
            for task in running:   # дрен отменён (остановка бота) — хендлеры тоже
                task.cancel()
    return stats
