# refresh.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config
from .formatter import make_topic_name
from .hde_api import HDEApiClient, HDEApiError, HDETicket
from .time_utils import to_storage, utcnow
from .topic_manager import sync_ticket_topic

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    active_before: int
    hde_count: int
    created: list = field(default_factory=list)
    renamed: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    cleaned_pending: int = 0
    active_after: int = 0


def _ticket_to_payload(ticket: HDETicket, existing: db.TicketTopic | None) -> dict:
    """Build a normalised payload dict from an HDETicket for use in topic_manager."""
    priority = (existing.priority if existing and existing.priority else None) or "medium"
    return {
        "ticket_id": ticket.ticket_id,
        "unique_id": ticket.unique_id,
        "ticket_name": ticket.title,
        "company_name": ticket.company_name,
        "priority": priority,
        "status": "open",
        "owner_id": ticket.owner_id,
        "owner_name": config.hde_owner_name,
        "link": ticket.hde_link,
        "department": "",
        "date_update": "",
        "last_post_date": "",
        "sla_remaining_minutes": None,
        "attachments": [],
        "event_key": "",
        "message": "",
        "user_name": "",
    }


def _topic_display_name(ticket: HDETicket, existing: db.TicketTopic | None) -> str:
    priority = (existing.priority if existing and existing.priority else None) or "medium"
    return make_topic_name(ticket.unique_id, ticket.company_name, ticket.title, priority)


async def refresh_topics(bot: Bot) -> RefreshResult:
    """
    Full sync of Telegram forum topics with HDE open tickets assigned to me:
    - Create topics for tickets not yet in Telegram.
    - Rename topics where the ticket title / company changed.
    - Delete topics for tickets no longer open / assigned to me.
    - Clean up pending-delete topics.
    """
    active_topics = await db.list_active_topics()
    active_before = len(active_topics)

    client = HDEApiClient()
    hde_tickets = await client.get_my_open_tickets()
    hde_count = len(hde_tickets)

    # Build lookup from HDE (by ticket_id and unique_id)
    hde_by_ticket_id: dict[str, HDETicket] = {t.ticket_id: t for t in hde_tickets}
    hde_by_unique_id: dict[str, HDETicket] = {
        t.unique_id: t for t in hde_tickets if t.unique_id and t.unique_id != t.ticket_id
    }

    # Build lookup from DB (active + pending_delete)
    pending_topics = await db.list_topics_by_state("pending_delete")
    all_db_topics = active_topics + pending_topics
    db_by_ticket_id: dict[str, db.TicketTopic] = {t.ticket_id: t for t in all_db_topics}
    db_by_unique_id: dict[str, db.TicketTopic] = {
        t.unique_id: t for t in all_db_topics if t.unique_id
    }

    logger.info(
        "refresh: DB active=%d pending=%d, HDE open=%d",
        active_before, len(pending_topics), hde_count,
    )

    created: list[HDETicket] = []
    renamed: list[HDETicket] = []

    # ── Step 0: probe active/pending topics to detect manually-deleted ones ───
    # edit_forum_topic is the lightest probe that throws "thread not found"
    # when a topic was manually deleted without the bot being notified.
    for topic in all_db_topics:
        ticket = (
            hde_by_ticket_id.get(topic.ticket_id)
            or hde_by_unique_id.get(topic.unique_id or "")
        )
        # Probe EVERY db topic against Telegram — independent of HDE presence.
        # When HDE returns 0 tickets (no open tickets, or reassigned away),
        # gating the probe on `ticket in HDE` left manually-deleted topics
        # undetected forever. Build the probe name from the DB record when
        # the ticket is no longer in HDE.
        if ticket is not None:
            probe_name = _topic_display_name(ticket, topic)
        else:
            probe_name = make_topic_name(
                topic.unique_id or "",
                topic.company_name or "",
                topic.ticket_name or "",
                topic.priority or "medium",
            )
        try:
            await bot.edit_forum_topic(
                chat_id=topic.chat_id,
                message_thread_id=topic.topic_id,
                name=probe_name,
            )
        except TelegramAPIError as exc:
            err = str(exc).lower()
            if "topic_not_modified" in err:
                pass  # same name — topic alive, not an error
            elif any(k in err for k in ("thread not found", "topic_id_invalid", "not found", "deleted")):
                logger.warning(
                    "refresh: topic %d for ticket %s not found in Telegram, marking deleted",
                    topic.topic_id, topic.ticket_id,
                )
                await db.mark_topic_deleted(topic.ticket_id)
            # other errors (e.g. flood) — skip silently, step 1 will recreate

    # ── Step 1: upsert a topic for every open HDE ticket ──────────────────────
    for ticket in hde_tickets:
        existing = (
            db_by_ticket_id.get(ticket.ticket_id)
            or db_by_unique_id.get(ticket.unique_id)
            or db_by_ticket_id.get(ticket.unique_id)
        )

        will_create = existing is None or existing.is_deleted
        will_rename = False
        if not will_create and existing:
            old_name = make_topic_name(
                existing.unique_id or "",
                existing.company_name or "",
                existing.ticket_name or "",
                existing.priority or "medium",
            )
            new_name = _topic_display_name(ticket, existing)
            will_rename = old_name != new_name

        try:
            await sync_ticket_topic(bot, _ticket_to_payload(ticket, existing))
        except Exception as exc:
            logger.error("refresh: failed to sync ticket %s: %s", ticket.ticket_id, exc)
            continue

        if will_create:
            created.append(ticket)
            logger.info("refresh: created topic for ticket %s (%s)", ticket.ticket_id, ticket.title)
        elif will_rename:
            renamed.append(ticket)
            logger.info("refresh: renamed topic for ticket %s → %s", ticket.ticket_id, ticket.title)

    # ── Step 2: delete active topics whose tickets are gone from HDE ──────────
    deleted: list[tuple[str, str, str]] = []  # (ticket_name, ticket_id, link)
    for topic in active_topics:
        in_hde = (
            topic.ticket_id in hde_by_ticket_id
            or topic.unique_id in hde_by_unique_id
            or topic.ticket_id in hde_by_unique_id
        )
        if not in_hde:
            verify = await client.get_ticket_open_status(topic.ticket_id)
            if verify is None:
                logger.warning(
                    "refresh: skip deletion for ticket %s (HDE verify failed, fail-safe)",
                    topic.ticket_id,
                )
                continue
            is_deletable, link = verify
            if not is_deletable:
                logger.info(
                    "refresh: skip deletion for ticket %s (status not closed/resolved)",
                    topic.ticket_id,
                )
                continue
            await db.update_topic(
                topic.ticket_id,
                topic_state="deleted",
                deleted_at=to_storage(utcnow()),
            )
            try:
                await bot.delete_forum_topic(
                    chat_id=topic.chat_id,
                    message_thread_id=topic.topic_id,
                )
            except TelegramAPIError as exc:
                logger.warning("refresh: could not delete topic %d: %s", topic.topic_id, exc)
            deleted.append((topic.ticket_name or topic.ticket_id, topic.ticket_id, link))
            logger.info("refresh: deleted stale topic %d for ticket %s", topic.topic_id, topic.ticket_id)

    # ── Step 3: clean up remaining pending-delete topics ─────────────────────
    cleaned_pending = 0
    for topic in pending_topics:
        # Skip ones that were just reopened in step 1
        refreshed = await db.get_topic(topic.ticket_id)
        if refreshed and not refreshed.is_pending_delete:
            continue
        try:
            await bot.delete_forum_topic(
                chat_id=topic.chat_id,
                message_thread_id=topic.topic_id,
            )
            cleaned_pending += 1
        except TelegramAPIError as exc:
            logger.warning("refresh: could not delete pending topic %d: %s", topic.topic_id, exc)
        await db.update_topic(
            topic.ticket_id,
            topic_state="deleted",
            deleted_at=to_storage(utcnow()),
        )

    # ── Step 4: purge orphaned Telegram topics (DB state=deleted but TG topic may
    #           still exist — e.g. deletion failed during testing/crashes) ────────
    deleted_db_topics = await db.list_topics_by_state("deleted")
    purged_orphans = 0
    for topic in deleted_db_topics:
        try:
            await bot.delete_forum_topic(
                chat_id=topic.chat_id,
                message_thread_id=topic.topic_id,
            )
            purged_orphans += 1
            logger.info("refresh: purged orphaned topic %d (ticket %s)", topic.topic_id, topic.ticket_id)
        except TelegramAPIError:
            pass  # already deleted — that's fine

    active_after = await db.count_active_topics()

    return RefreshResult(
        active_before=active_before,
        hde_count=hde_count,
        created=created,
        renamed=renamed,
        deleted=deleted,
        cleaned_pending=cleaned_pending,
        active_after=active_after,
    )
