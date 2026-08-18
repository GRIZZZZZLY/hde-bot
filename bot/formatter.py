import re
from datetime import datetime as _dt
from html import escape, unescape
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .hde_api import HDEPost, HDETicketInfo


PRIORITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}


def priority_emoji(priority: str) -> str:
    return PRIORITY_EMOJI.get((priority or "").lower(), "🟡")


def _escape(value: Optional[str]) -> str:
    return escape(value or "")


def _sla_text(sla_remaining: Optional[str]) -> str:
    if not sla_remaining:
        return ""
    try:
        minutes = int(sla_remaining)
    except (TypeError, ValueError):
        return str(sla_remaining)

    hours, mins = divmod(abs(minutes), 60)
    if hours > 0:
        return f"{hours}ч {mins}мин" if mins else f"{hours}ч"
    return f"{mins}мин"


def make_topic_name(
    display_id: str,
    company: str,
    ticket_name: str,
    priority: str = "medium",
) -> str:
    name = f"{priority_emoji(priority)} {ticket_name}"
    return name[:128]


def format_assignment_message(
    display_id: str,
    company_name: str,
    ticket_name: str,
    status: str,
    priority: str,
    link: str,
    *,
    is_reassignment: bool = False,
) -> str:
    title = "🆕 <b>Тикет снова назначен на вас</b>" if is_reassignment else "🆕 <b>Тикет назначен на вас</b>"
    lines = [
        title,
        f"🎫 <b>#{_escape(display_id)}</b>",
        f"📝 {_escape(ticket_name)}",
    ]
    if status:
        lines.append(f"📌 {_escape(status)}")
    if priority:
        lines.append(f"{priority_emoji(priority)} {_escape(priority)}")
    lines.append(f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>')
    return "\n".join(lines)


def format_ticket_renamed(old_name: str, new_name: str) -> str:
    return (
        "✏️ <b>Название тикета обновлено</b>\n"
        f"Было: {_escape(old_name)}\n"
        f"Стало: {_escape(new_name)}"
    )


def format_client_reply(
    user_name: str,
    message: str,
    sla_remaining: Optional[str],
    link: str,
    date_str: str = "",
) -> str:
    actor = _escape(user_name) if user_name else "Клиент"
    body = _escape(message) if message else "Без текста"
    date = f" · {_parse_hde_post_date(date_str)}" if date_str else ""
    return (
        f"👤 <b>{actor}</b>{date}\n"
        f"<blockquote>{body}</blockquote>\n"
        f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>'
    )


def format_staff_reply(user_name: str, link: str) -> str:
    actor = _escape(user_name) if user_name else "Сотрудник"
    return (
        "✅ <b>Ответ сотрудника отправлен</b>\n"
        f"👤 {actor}\n"
        f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>'
    )


def format_unassigned_message(display_id: str, link: str) -> str:
    return (
        "⏳ <b>Тикет снят с вас</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>\n"
        "🗑️ Topic будет удален через 8 часов, если тикет не вернется\n"
        f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>'
    )


def format_pre_sla_alert_topic(
    minutes_left: int,
    ticket_name: str,
    link: str,
) -> str:
    time_label = "меньше 1 мин" if minutes_left == 0 else f"{minutes_left} мин"
    return (
        f"🔥 SLA через {time_label} 🔥\n"
        "──────────────\n"
        f"📝 {_escape(ticket_name)}\n"
        f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>'
    )


def format_pre_sla_alert_general(
    minutes_left: int,
    ticket_name: str,
    company_name: str,
    link: str,
) -> str:
    time_label = "меньше 1 минуты" if minutes_left == 0 else f"{minutes_left} минут"
    return (
        f"🆘 SLA через {time_label} — тикет не назначен!\n"
        "──────────────\n"
        f"📝 {_escape(ticket_name)}\n"
        f"🏢 {_escape(company_name)}\n"
        f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>'
    )


def format_note_saved(display_id: str) -> str:
    return (
        "📝 <b>Внутренний комментарий сохранен</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>"
    )


def format_reply_draft(display_id: str, text: str) -> str:
    return (
        "✉️ <b>Черновик публичного ответа</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>\n"
        f"💬 {_escape(text)}\n\n"
        "Команды:\n"
        "/send — отправить клиенту\n"
        "/cancel — отменить"
    )


def format_reply_sent(display_id: str) -> str:
    return (
        "✅ <b>Публичный ответ отправлен в HDE</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>"
    )


def format_reply_cancelled(display_id: str) -> str:
    return (
        "🗑️ <b>Черновик удален</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>"
    )


def format_operator_error(message: str) -> str:
    return f"⚠️ <b>{_escape(message)}</b>"


def format_ticket_closed() -> str:
    return "🗑️ <b>Тикет закрыт, topic будет удален</b>"


def format_sla_alert(
    ticket_id: str,
    ticket_name: str,
    company_name: str,
    sla_remaining: Optional[str],
    link: str,
) -> str:
    minutes_left = _sla_text(sla_remaining)
    return (
        f"⏰ <b>До нарушения SLA осталось {minutes_left}</b>\n"
        f"🎫 <b>#{_escape(ticket_id)}</b>\n"
        f"🏢 {_escape(company_name)}\n"
        f"📝 {_escape(ticket_name)}\n"
        f'🔗 <a href="{_escape(link)}">Открыть в HDE</a>'
    )


def format_message_edited(display_id: str) -> str:
    return (
        "✏️ <b>Сообщение обновлено в HDE</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>"
    )


def format_message_deleted(display_id: str) -> str:
    return (
        "🗑️ <b>Сообщение удалено из HDE</b>\n"
        f"🎫 <b>#{_escape(display_id)}</b>"
    )


def format_morning_digest(unassigned_equipment_count: int = 0) -> str:
    return "\n".join([
        "📊 <b>Сводка за ночь</b>",
        "",
        f"⚠️ Неприсвоенных (Оборудование): <b>{unassigned_equipment_count}</b>",
    ])


def hde_ticket_url(ticket_id: str) -> str:
    """Ссылка на тикет в интерфейсе оператора. Пусто, если база API не настроена."""
    from .config import config
    base = (config.hde_api_base_url or "").strip().rstrip("/")
    if not base:
        return ""
    base = base.split("/api/")[0].rstrip("/")
    return f"{base}/ru/ticket/list/filter/id/1/ticket/{ticket_id}"


def _ticket_ref(ticket_id: str) -> str:
    tid = escape(str(ticket_id or ""))
    url = hde_ticket_url(tid)
    return f'<a href="{url}">#{tid}</a>' if url else f"#{tid}"


def _judge_reason(detail: str) -> str:
    """Из judge_detail («judge:bot_escalated оператор решил сам») — только причину."""
    text = (detail or "").strip()
    if text.startswith("judge:"):
        parts = text.split(None, 1)
        text = parts[1] if len(parts) > 1 else ""
    return text.strip()


def _divergence_block(title: str, rows: list) -> list[str]:
    lines = ["", f"<b>{title}</b>"]
    for row in rows:
        reason = escape(_judge_reason(row.get("judge_detail", ""))[:160])
        bot_a = escape((row.get("ai_answer") or "").strip())[:180]
        op_a = escape((row.get("judge_reference_answer") or "").strip())[:180]
        lines.append(f"{_ticket_ref(row.get('ticket_id', ''))} — {reason}")
        lines.append(f"   бот: {bot_a}")
        lines.append(f"   опер: {op_a}")
    return lines


def format_reconciliation_digest(data: dict) -> Optional[str]:
    """Утренняя сводка ночной сверки «бот ↔ оператор».

    Показывает только то, по чему есть что делать: эскалация вместо решения и
    неверный факт. None — если вердиктов не было вовсе; если они были, но все
    совпали, остаётся одна строка: полная тишина неотличима от несработавшей
    задачи."""
    judged = int(data.get("judged", 0) or 0)
    if judged == 0:
        return None
    counts = data.get("counts") or {}
    escalated = data.get("escalated") or []
    wrong_fact = data.get("wrong_fact") or []
    lines = ["🧭 <b>Сверка ответов за сутки</b>"]
    if not escalated and not wrong_fact:
        lines.append(f"Сверено: <b>{judged}</b> — расхождений нет.")
    else:
        lines.append(
            f"Сверено: <b>{judged}</b> · совпало {counts.get('same_action', 0)} · "
            f"эскалация вместо решения {counts.get('bot_escalated', 0)} · "
            f"неверный факт {counts.get('bot_wrong_fact', 0)}"
        )
        if wrong_fact:
            lines += _divergence_block("❌ Неверно по существу", wrong_fact)
        if escalated:
            lines += _divergence_block("🔁 Оператор решил сам", escalated)
    trend = data.get("trend") or []
    if len(trend) > 1:
        cells = " · ".join(
            f"{(t.get('day') or '')[5:]} {t.get('diverged', 0)}/{t.get('judged', 0)}"
            for t in trend
        )
        lines += ["", f"<i>Расхождений/сверено по дням: {cells}</i>"]
    return "\n".join(lines)


def _strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities."""
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def _parse_hde_post_date(date_str: str) -> str:
    """Convert HDE date formats → 'DD.MM HH:MM'.

    Handles:
    - "HH:MM:SS DD.MM.YYYY"  (posts/comments API)
    - "DD.MM.YYYY HH:MM"     (webhook last_post_date)
    """
    s = date_str.strip()
    for fmt in ("%H:%M:%S %d.%m.%Y", "%d.%m.%Y %H:%M"):
        try:
            return _dt.strptime(s, fmt).strftime("%d.%m %H:%M")
        except ValueError:
            continue
    return s[:16]


def format_ticket_history(
    posts: "list[HDEPost]",
    info: "HDETicketInfo",
    max_messages: int = 10,
) -> list[str]:
    """Return a list of formatted strings — one per post — to send as separate messages.

    Client posts: 👤  Staff posts: 🧑‍💼
    Body rendered in <blockquote> so Telegram shows the left-bar indent.
    """
    if not posts:
        return []

    shown = posts[-max_messages:] if len(posts) > max_messages else posts
    skipped = len(posts) - len(shown)

    result: list[str] = []

    if skipped:
        result.append(f"<i>· · · {skipped} более ранних сообщений · · ·</i>")

    for post in shown:
        if post.is_comment:
            icon = "🔒"
            name = escape(post.user_name or "Сотрудник")
        else:
            is_client = post.user_id == info.client_id
            icon = "👤" if is_client else "🧑‍💼"
            fallback = info.client_name if is_client else info.owner_name
            name = escape(post.user_name or fallback)
        date = _parse_hde_post_date(post.date_created)
        body = _strip_html(post.text)
        if not body:
            continue
        # Truncate very long messages
        if len(body) > 800:
            body = body[:800] + "…"
        result.append(
            f"{icon} <b>{name}</b> · {date}\n"
            f"<blockquote>{escape(body)}</blockquote>"
        )

    return result


def format_refresh_result(
    active_count: int,
    hde_count: int,
    created: list = (),
    renamed: list = (),
    deleted: list = (),
    cleaned_pending: int = 0,
    # legacy alias kept for backwards compat
    marked_deleted: list = (),
) -> str:
    # support old callers that pass marked_deleted
    if marked_deleted and not deleted:
        deleted = marked_deleted

    lines = [
        "🔄 <b>Синхронизация завершена</b>",
        "",
        f"📡 Тикетов в HDE: <b>{hde_count}</b>",
        f"✅ Активных топиков: <b>{active_count}</b>",
    ]
    if created:
        lines.append(f"➕ Создано топиков: <b>{len(created)}</b>")
        for t in created:
            title = _escape(getattr(t, "title", "") or getattr(t, "ticket_name", "") or getattr(t, "ticket_id", ""))
            company = _escape(getattr(t, "company_name", ""))
            lines.append(f"  • {title} — {company}")
    if renamed:
        lines.append(f"✏️ Переименовано: <b>{len(renamed)}</b>")
        for t in renamed:
            title = _escape(getattr(t, "title", "") or getattr(t, "ticket_name", "") or getattr(t, "ticket_id", ""))
            lines.append(f"  • {title}")
    if deleted:
        lines.append(f"🗑️ Удалено устаревших: <b>{len(deleted)}</b>")
        for item in deleted:
            if isinstance(item, tuple):
                name, _ticket_id, link = item
                name = _escape(name)
                if link:
                    lines.append(f'  • {name} · <a href="{link}">Открыть в HDE</a>')
                else:
                    lines.append(f"  • {name}")
            else:
                name = _escape(getattr(item, "ticket_name", "") or getattr(item, "ticket_id", ""))
                lines.append(f"  • {name}")
    if cleaned_pending:
        lines.append(f"🧹 Очищено закрытых топиков: <b>{cleaned_pending}</b>")
    if not created and not renamed and not deleted and not cleaned_pending:
        lines.append("✨ Всё актуально, расхождений нет")
    return "\n".join(lines)


def format_client_history(
    client_name: str,
    total: int,
    recent_titles: list[str],
    last_ticket_date: Optional[str],
) -> str:
    """Format client past-tickets summary for display in a topic.

    Returns empty string when total == 0 (caller should skip sending).
    """
    if total == 0:
        return ""

    titles_block = "\n".join(f"• {_escape(t)}" for t in recent_titles) if recent_titles else ""
    last_line = f"\n🕐 Последнее: {_escape(last_ticket_date)}" if last_ticket_date else ""

    return (
        f"🏢 <b>Клиент: {_escape(client_name)}</b> — {total} обращений\n\n"
        f"📋 Последние темы:\n{titles_block}"
        f"{last_line}"
    )
