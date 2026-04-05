# refresh.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config
from .hde_api import HDEApiClient, HDEApiError
from .time_utils import to_storage, utcnow

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    active_before: int
    hde_count: int
    marked_deleted: list = field(default_factory=list)
    cleaned_pending: int = 0
    active_after: int = 0


async def refresh_topics(bot: Bot) -> RefreshResult:
    """
    Sync active topics in DB with HDE API.
    - Active topics not in HDE → mark deleted + delete Telegram topic.
    - Pending-delete topics → delete Telegram topic + mark deleted.
    """
    active_topics = await db.list_active_topics()
    active_before = len(active_topics)

    client = HDEApiClient()
    hde_tickets = await client.get_my_open_tickets()
    hde_count = len(hde_tickets)

    hde_ticket_ids = {t.ticket_id for t in hde_tickets}
    hde_unique_ids = {t.unique_id for t in hde_tickets}

    logger.info(
        "refresh: DB active=%d, HDE open=%d | HDE ids=%s",
        active_before,
        hde_count,
        hde_ticket_ids,
    )

    # 1. Mark stale active topics as deleted
    marked_deleted: list[db.TicketTopic] = []
    for topic in active_topics:
        in_hde = (
            topic.ticket_id in hde_ticket_ids
            or topic.unique_id in hde_unique_ids
            or topic.ticket_id in hde_unique_ids
        )
        logger.info(
            "refresh: topic ticket_id=%r unique_id=%r → in_hde=%s",
            topic.ticket_id,
            topic.unique_id,
            in_hde,
        )
        if not in_hde:
            await db.update_topic(
                topic.ticket_id,
                topic_state="deleted",
                deleted_at=to_storage(utcnow()),
            )
            try:
                await bot.delete_forum_topic(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic.topic_id,
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not delete topic %s during refresh: %s", topic.topic_id, exc
                )
            marked_deleted.append(topic)

    # 2. Clean up pending-delete topics (already closed in Telegram but not deleted)
    pending_topics = await db.list_topics_by_state("pending_delete")
    cleaned_pending = 0
    for topic in pending_topics:
        try:
            await bot.delete_forum_topic(
                chat_id=config.group_chat_id,
                message_thread_id=topic.topic_id,
            )
            cleaned_pending += 1
        except TelegramAPIError as exc:
            logger.warning(
                "Could not delete pending topic %s during refresh: %s", topic.topic_id, exc
            )
        await db.update_topic(
            topic.ticket_id,
            topic_state="deleted",
            deleted_at=to_storage(utcnow()),
        )

    active_after_list = await db.list_active_topics()

    return RefreshResult(
        active_before=active_before,
        hde_count=hde_count,
        marked_deleted=marked_deleted,
        cleaned_pending=cleaned_pending,
        active_after=len(active_after_list),
    )
