"""Implicit feedback learning from operator replies.

Mechanically extracted from bot.topic_manager (pure move, no behavior change).
Shared state and test patch-points (db, _maybe_update_pattern) are accessed
late-bound through the bot.topic_manager module object so monkeypatches on
bot.topic_manager keep working.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _implicit_feedback(record: "db.TicketTopic", staff_text: str) -> None:
    """Auto-learn from diff between AI suggestion and operator's actual reply."""
    import difflib
    import re as _re
    from html import unescape

    from . import topic_manager as _tm

    pending = await _tm.db.get_ai_feedback_pending(record.topic_id)
    if not pending:
        return

    ai_text = (pending.get("answer_text") or "").strip()
    # Strip HTML from staff reply
    clean_staff = unescape(_re.sub(r"<[^>]+>", "", staff_text)).strip()
    if not ai_text or not clean_staff:
        return

    ratio = difflib.SequenceMatcher(None, ai_text.lower(), clean_staff.lower()).ratio()
    logger.debug(
        "Implicit feedback for topic %d: ratio=%.2f ai=%r staff=%r",
        record.topic_id, ratio, ai_text[:60], clean_staff[:60],
    )

    if ratio >= 0.7:
        # Operator sent nearly the same text — AI suggestion was good
        await _tm.db.delete_ai_feedback_pending(record.topic_id)
        content = f"Тема: {pending['title']}\n\n{pending['history']}"
        # Удалить старый implicit_good для этого тикета (один тикет = одна запись)
        try:
            await _tm.db.delete_knowledge_item_by_ticket(
                pending["ticket_id"], "implicit_good"
            )
        except Exception as exc:
            logger.warning("delete_knowledge_item_by_ticket failed: %s", exc)
        from .knowledge.indexer import index_knowledge_item
        await index_knowledge_item(
            source="implicit_good",
            content=content,
            ticket_id=pending["ticket_id"],
            title=pending["title"],
            quality="good",
        )
        logger.info(
            "Implicit 👍 for ticket %s (ratio=%.2f)", pending["ticket_id"], ratio
        )
        # Save sample for prompt optimizer
        try:
            await _tm.db.save_optimization_sample(
                ticket_id=pending["ticket_id"],
                title=pending.get("title", ""),
                history=pending.get("history", ""),
                ai_answer=pending.get("answer_text", ""),
                op_answer=clean_staff,
                outcome="accepted",
            )
        except Exception as exc:
            logger.warning("save_optimization_sample failed: %s", exc)
        # Reinforce or create solution pattern (only for high-confidence matches)
        if ratio >= 0.85:
            try:
                await _tm._maybe_update_pattern(pending["title"], clean_staff, pending["ticket_id"])
            except Exception as exc:
                logger.warning("Pattern update failed: %s", exc)
    elif ratio <= 0.35:
        # Operator wrote something significantly different — save as correction
        await _tm.db.delete_ai_feedback_pending(record.topic_id)
        content = (
            f"Тема: {pending['title']}\n\n"
            f"{pending['history']}\n\n"
            f"Правильный ответ: {clean_staff}"
        )
        from .knowledge.indexer import index_knowledge_item
        await index_knowledge_item(
            source="implicit_corrected",
            content=content,
            ticket_id=pending["ticket_id"],
            title=pending["title"],
            quality="corrected",
        )
        logger.info(
            "Implicit ✏️ for ticket %s (ratio=%.2f)", pending["ticket_id"], ratio
        )
        # Save sample for prompt optimizer
        try:
            await _tm.db.save_optimization_sample(
                ticket_id=pending["ticket_id"],
                title=pending.get("title", ""),
                history=pending.get("history", ""),
                ai_answer=pending.get("answer_text", ""),
                op_answer=clean_staff,
                outcome="corrected",
            )
        except Exception as exc:
            logger.warning("save_optimization_sample failed: %s", exc)
        # Update wiki article with operator's actual answer (non-fatal)
        try:
            from .wiki.builder import build_or_update_wiki_article
            await build_or_update_wiki_article(
                title=pending["title"],
                content=content,
                ticket_id=pending["ticket_id"],
            )
        except Exception as exc:
            logger.warning("Wiki update failed after implicit correction: %s", exc)
    # 0.35–0.7: ambiguous edit, skip to avoid noise
