"""Ежедневный отчёт пользы AI-подсказок (ревизия 3 roadmap).

Метрики пользы оператору — не LLM-оценки: сколько подсказок отправлено без
правки, сколько исправлено/отклонено, время до отправки. Данные собирает
фаза 0A (ai_suggestions + события кнопок); отчёт уходит утром в личку.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def format_value_report(stats: dict) -> str:
    total = stats.get("total", 0)
    sent_no_edit = stats.get("sent_no_edit", 0)
    sent_edited = stats.get("sent_edited", 0)
    edited = stats.get("edited", 0)
    rejected = stats.get("rejected", 0)
    approved = stats.get("approved", 0)
    unused = stats.get("unused", 0)
    sent_total = sent_no_edit + sent_edited

    lines = [
        "📊 <b>AI-подсказки за сутки</b>",
        f"Всего подсказок: {total}",
        f"📤 отправлено: {sent_total} (без правки: {sent_no_edit})",
        f"✏️ исправлено: {edited} · 👍 одобрено: {approved} · 👎 отклонено: {rejected}",
        f"Без реакции: {unused}",
    ]
    if sent_total:
        share = round(100 * sent_no_edit / sent_total)
        lines.append(f"Доля 📤 без правки: {share}%")
    avg_min = stats.get("avg_minutes_to_send")
    if avg_min is not None:
        lines.append(f"⏱ Среднее время до отправки: {round(avg_min)} мин")
    return "\n".join(lines)


async def send_daily_value_report(bot, *, _stats_fn=None) -> bool:
    """Отправляет отчёт в личку оператора. False — если данных нет (не спамим)."""
    if _stats_fn is None:
        from ..db import collect_suggestion_daily_stats as _stats_fn
    from ..config import config

    stats = await _stats_fn(24)
    if not stats or not stats.get("total"):
        return False
    text = format_value_report(stats)
    try:
        await bot.send_message(config.personal_chat_id, text, parse_mode="HTML")
        return True
    except Exception as exc:
        logger.warning("daily value report send failed: %s", exc)
        return False
