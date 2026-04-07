import zoneinfo
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
from html import escape
from typing import Optional


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
) -> str:
    actor = _escape(user_name) if user_name else "Клиент"
    body = _escape(message) if message else "Без текста"
    return (
        f"📩 <b>Ответ клиента</b> · {actor}\n"
        "──────────────\n"
        f"{body}\n\n"
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


def format_pre_sla_alert(
    display_id: str,
    ticket_name: str,
    company_name: str,
    minutes_left: int,
    link: str,
) -> str:
    return (
        f"🔥 <b>Тикет сгорит через {minutes_left} минут</b> 🔥\n"
        "──────────────\n"
        f"📝 {_escape(ticket_name)}\n"
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


def _parse_hde_sla_date(sla_date: Optional[str]) -> Optional[_dt]:
    """Parse HDE sla_date format: 'DD.MM.YYYY HH:MM' (Moscow time UTC+3)."""
    if not sla_date or sla_date == "null":
        return None
    try:
        msk = zoneinfo.ZoneInfo("Europe/Moscow")
        dt = _dt.strptime(sla_date, "%d.%m.%Y %H:%M")
        return dt.replace(tzinfo=msk).astimezone(_tz.utc)
    except (ValueError, KeyError):
        return None


def _sla_remaining_text(sla_date: Optional[str]) -> Optional[str]:
    """Return human-readable time until SLA, or None if SLA already passed."""
    dt = _parse_hde_sla_date(sla_date)
    if dt is None:
        return None
    now = _dt.now(_tz.utc)
    delta = dt - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return None  # already past — skip in digest
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}ч {minutes}мин" if minutes else f"{hours}ч"
    return f"{minutes}мин"


def format_morning_digest(
    night_start_label: str,
    night_end_label: str,
    assigned_tickets: list,
    total_open: int,
    open_tickets_with_sla: list,
    unassigned_equipment_count: int = 0,
) -> str:
    lines = [
        "📊 <b>Сводка за ночь</b>",
        f"🕕 {night_start_label} — {night_end_label}",
        "",
        f"📥 Назначено за ночь: <b>{len(assigned_tickets)}</b>",
        f"🟢 Открытых тикетов сейчас: <b>{total_open}</b>",
        f"⚠️ Неприсвоенных (Оборудование): <b>{unassigned_equipment_count}</b>",
    ]

    if assigned_tickets:
        lines.append("")
        for t in assigned_tickets:
            ticket_line = f"• {_escape(t.ticket_name)}"
            if t.hde_link:
                ticket_line += f' <a href="{_escape(t.hde_link)}">🔗</a>'
            lines.append(ticket_line)

    # SLA section — show tickets with upcoming SLA sorted by sla_date ascending
    sla_lines = []
    for ticket in open_tickets_with_sla:
        remaining = _sla_remaining_text(getattr(ticket, "sla_date", None))
        if remaining is None:
            continue
        line = f"• {_escape(ticket.title)} — {_escape(ticket.company_name)} — {remaining}"
        line += f' <a href="{_escape(ticket.link_staff)}">🔗</a>'
        sla_lines.append(line)

    if sla_lines:
        lines.append("")
        lines.append("⏰ <b>Очередь по SLA:</b>")
        lines.extend(sla_lines)

    return "\n".join(lines)


def format_refresh_result(
    active_count: int,
    hde_count: int,
    marked_deleted: list,
    cleaned_pending: int = 0,
) -> str:
    lines = [
        "🔄 <b>Синхронизация завершена</b>",
        "",
        f"📡 Тикетов в HDE: <b>{hde_count}</b>",
        f"✅ Активных топиков: <b>{active_count}</b>",
    ]
    if marked_deleted:
        lines.append(f"🗑️ Удалено устаревших: <b>{len(marked_deleted)}</b>")
        for t in marked_deleted:
            name = _escape(getattr(t, "ticket_name", "") or getattr(t, "ticket_id", ""))
            company = _escape(getattr(t, "company_name", ""))
            lines.append(f"  • {name} — {company}")
    if cleaned_pending:
        lines.append(f"🧹 Очищено закрытых топиков: <b>{cleaned_pending}</b>")
    if not marked_deleted and not cleaned_pending:
        lines.append("✨ Всё актуально, расхождений нет")
    return "\n".join(lines)
