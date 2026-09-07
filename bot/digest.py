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

    Если сверять было нечего — молчит. Очередь кандидатов в базу знаний к этому
    моменту уже разобрана автоматически (см. agent/kb_distill.py): в сводку идёт
    строка с итогами, а отдельными сообщениями с кнопками — только противоречия.
    """
    from .formatter import format_kb_outcome_line, format_reconciliation_digest
    data = await db.get_reconciliation_digest(hours=24)
    text = format_reconciliation_digest(data)
    kb_line = format_kb_outcome_line(await db.get_kb_candidate_stats(hours=24))
    if kb_line:
        text = f"{text}\n\n{kb_line}" if text else kb_line
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
    await send_kb_conflicts(bot)


async def send_kb_conflicts(bot: Bot, *, _list_fn=None) -> None:
    """Противоречия правил — единственное, что осталось решать человеку.

    Отдельными сообщениями, потому что решение принимается по каждому: кнопки в
    Telegram живут на сообщении, а не на строке внутри него. Остальные исходы
    разбора (правило добавлено, дубль, не обобщается) видны строкой в сводке."""
    import json
    from .formatter import _ticket_ref
    from .handlers.ai_feedback import kb_conflict_kb
    if _list_fn is None:
        from .db import list_conflict_kb_candidates as _list_fn
    from html import escape

    for candidate in await _list_fn(limit=10):
        try:
            rule = json.loads(candidate.get("rule_json") or "{}")
        except Exception:
            rule = {}
        text = "\n".join([
            f"⚠️ <b>Противоречие в базе знаний</b> "
            f"{_ticket_ref(candidate.get('ticket_id', ''))}",
            f"новое: {escape((rule.get('rule') or '').strip())[:300]}",
            f"опер: {escape((candidate.get('reference_answer') or '').strip())[:300]}",
            f"<i>{escape((candidate.get('reason') or '')[:200])}</i>",
        ])
        try:
            await bot.send_message(
                chat_id=config.personal_chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb_conflict_kb(candidate["id"]),
            )
        except TelegramAPIError as exc:
            logger.error("Failed to send KB conflict %s: %s", candidate.get("id"), exc)
