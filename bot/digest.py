# digest.py
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import db
from .config import config
from .formatter import format_morning_digest

logger = logging.getLogger(__name__)


async def send_morning_digest(bot: Bot) -> None:
    unassigned_equipment_count = await db.count_general_messages()

    text = format_morning_digest(
        unassigned_equipment_count=unassigned_equipment_count,
    )

    try:
        await bot.send_message(
            chat_id=config.group_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Morning digest sent: %d unassigned equipment", unassigned_equipment_count)
    except TelegramAPIError as exc:
        logger.error("Failed to send morning digest: %s", exc)


async def send_reconciliation_digest(bot: Bot) -> None:
    """Утренняя сводка ночной сверки «бот ↔ оператор» — в ЛИЧНЫЙ чат админа.
    Если сверять было нечего — молчит."""
    from .formatter import format_reconciliation_digest
    data = await db.get_reconciliation_digest(hours=24)
    text = format_reconciliation_digest(data)
    if text is None:
        return
    try:
        await bot.send_message(
            chat_id=config.personal_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(
            "Reconciliation digest sent: judged=%s categories=%s",
            data.get("judged"), data.get("counts"),
        )
    except TelegramAPIError as exc:
        logger.error("Failed to send reconciliation digest: %s", exc)
