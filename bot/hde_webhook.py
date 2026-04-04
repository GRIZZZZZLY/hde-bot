from __future__ import annotations

import hashlib
import json
import logging
from typing import Callable

from aiohttp import web
from aiogram import Bot

from . import db
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

DEDUPED_EVENT_TYPES = {
    "client_reply",
    "staff_reply",
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
        "date_update": str(payload.get("date_update") or "").strip(),
        "last_post_date": str(payload.get("last_post_date") or "").strip(),
        "sla_remaining_minutes": payload.get("sla_remaining_minutes", payload.get("sla_remaining")),
        "attachments": extract_client_attachment_refs(payload),
        "event_key": str(payload.get("event_key") or "").strip(),
    }


def _build_event_key(payload: dict) -> str:
    source = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def hde_webhook_handler(request: web.Request) -> web.Response:
    try:
        raw_payload = await request.json()
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Invalid JSON in HDE webhook: %s", exc)
        return web.Response(status=400, text="Invalid JSON")

    if config.hde_webhook_secret:
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

    should_dedupe = event_type in DEDUPED_EVENT_TYPES
    event_key = payload["event_key"] or _build_event_key(payload)
    if should_dedupe and await db.was_processed(event_key):
        logger.info("Skipping duplicate event %s for ticket %s", event_type, payload["ticket_id"])
        return web.Response(status=200, text="Duplicate OK")

    bot: Bot = request.app["bot"]
    handler = HANDLERS[event_type]

    try:
        await handler(bot, payload)
    except Exception as exc:
        logger.exception("Error handling event '%s': %s", event_type, exc)
        return web.Response(status=500, text="Internal error")

    if should_dedupe:
        await db.save_processed_event(event_key, event_type, payload["ticket_id"])
    logger.info("Processed %s for ticket %s", event_type, payload["ticket_id"])
    return web.Response(status=200, text="OK")
