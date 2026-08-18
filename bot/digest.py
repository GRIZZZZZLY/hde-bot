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
    Если сверять было нечего — молчит. Кандидаты в базу знаний идут отдельными
    сообщениями с кнопками, даже когда сводке сказать нечего."""
    from .formatter import format_reconciliation_digest
    data = await db.get_reconciliation_digest(hours=24)
    text = format_reconciliation_digest(data)
    if text is not None:
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
    await send_kb_candidates(bot)


async def send_kb_candidates(bot: Bot, *, _list_fn=None) -> None:
    """По одному сообщению на кандидата в базу знаний — с кнопками «в базу / мимо».

    Отдельными сообщениями, потому что решение принимается по каждому: кнопки в
    Telegram живут на сообщении, а не на строке внутри него."""
    from .formatter import _ticket_ref
    from .handlers.ai_feedback import kb_candidate_kb
    if _list_fn is None:
        from .db import list_pending_kb_candidates as _list_fn
    from html import escape

    for candidate in await _list_fn(limit=10):
        text = "\n".join([
            f"📚 <b>В базу знаний?</b> {_ticket_ref(candidate.get('ticket_id', ''))}",
            f"<i>{escape((candidate.get('reason') or '')[:200])}</i>",
            f"бот: {escape((candidate.get('ai_answer') or '').strip())[:300]}",
            f"опер: {escape((candidate.get('reference_answer') or '').strip())[:400]}",
        ])
        try:
            await bot.send_message(
                chat_id=config.personal_chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb_candidate_kb(candidate["id"]),
            )
        except TelegramAPIError as exc:
            logger.error("Failed to send KB candidate %s: %s", candidate.get("id"), exc)
