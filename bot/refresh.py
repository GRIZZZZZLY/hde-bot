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
    marked_deleted: list = field(default_factory=list)
    pending_delete_count: int = 0
    active_after: int = 0


async def refresh_topics(bot: Bot) -> RefreshResult:
    """
    Sync active topics in DB with HDE API.
    Topics that are 'active' in DB but not returned by HDE as open/assigned
    get marked as 'deleted' and their Telegram topics are closed.
    """
    active_topics = await db.list_active_topics()
    active_before = len(active_topics)

    client = HDEApiClient()
    hde_tickets = await client.get_my_open_tickets()

    # Build sets of IDs that HDE says are open and assigned to me
    hde_ticket_ids = {t.ticket_id for t in hde_tickets}
    hde_unique_ids = {t.unique_id for t in hde_tickets}

    marked_deleted: list[db.TicketTopic] = []
    for topic in active_topics:
        in_hde = (
            topic.ticket_id in hde_ticket_ids
            or topic.unique_id in hde_unique_ids
            or topic.ticket_id in hde_unique_ids
        )
        if not in_hde:
            await db.update_topic(
                topic.ticket_id,
                topic_state="deleted",
                deleted_at=to_storage(utcnow()),
            )
            try:
                await bot.close_forum_topic(
                    chat_id=config.group_chat_id,
                    message_thread_id=topic.topic_id,
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not close topic %s during refresh: %s", topic.topic_id, exc
                )
            marked_deleted.append(topic)

    active_after_list = await db.list_active_topics()
    pending = await db.count_pending_delete_topics()

    return RefreshResult(
        active_before=active_before,
        marked_deleted=marked_deleted,
        pending_delete_count=pending,
        active_after=len(active_after_list),
    )
