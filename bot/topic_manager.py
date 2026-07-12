from __future__ import annotations

import asyncio
import logging
import weakref
from datetime import datetime, timedelta
from typing import Optional

# Weak values: an entry lives only while some coroutine holds the lock object
# (e.g. inside `async with _ticket_lock(id):`), so the dict can't grow forever.
_ticket_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _ticket_lock(ticket_id: str) -> asyncio.Lock:
    lock = _ticket_locks.get(ticket_id)
    if lock is None:
        lock = asyncio.Lock()
        _ticket_locks[ticket_id] = lock
    return lock

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo

from . import db
from .agent.pipeline import run_agent
from .ai_summary import generate_ticket_summary
from .client_media import detect_telegram_media_kind, download_client_attachment
from .config import config
from .formatter import (
    format_assignment_message,
    format_client_history,
    format_client_reply,
    format_pre_sla_alert_general,
    format_pre_sla_alert_topic,
    format_ticket_history,
    format_ticket_renamed,
    format_unassigned_message,
    make_topic_name,
)
from .time_utils import parse_datetime, to_storage, utcnow

logger = logging.getLogger(__name__)

PRIORITY_COLORS = {
    "critical": 0xFB6F5F,
    "high": 0xFFD67E,
    "medium": 0x6FB9F0,
    "low": 0x8EEE98,
}


def _priority_color(priority: str) -> int:
    return PRIORITY_COLORS.get((priority or "").lower(), 0x6FB9F0)


def _display_id(payload: dict) -> str:
    return str(payload.get("unique_id") or payload.get("ticket_id") or "")


def _payload_value(payload: dict, key: str) -> str:
    return str(payload.get(key) or "").strip()


def _build_topic_name(payload: dict) -> str:
    return make_topic_name(
        _display_id(payload),
        _payload_value(payload, "company_name"),
        _payload_value(payload, "ticket_name"),
        _payload_value(payload, "priority") or "medium",
    )


def _topic_metadata(payload: dict) -> dict:
    return {
        "unique_id": _display_id(payload),
        "company_name": _payload_value(payload, "company_name"),
        "ticket_name": _payload_value(payload, "ticket_name"),
        "priority": _payload_value(payload, "priority"),
        "status": _payload_value(payload, "status"),
        "owner_id": _payload_value(payload, "owner_id"),
        "owner_name": _payload_value(payload, "owner_name"),
        "hde_link": _payload_value(payload, "link"),
    }


def _chunked(items: list, size: int) -> list[list]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _effective_owner_match(payload: dict) -> bool:
    return config.matches_owner(
        _payload_value(payload, "owner_id"),
        _payload_value(payload, "owner_name"),
    )


def _relative_date(date_str: str | None) -> str | None:
    """Convert HDE date string to Russian relative label.

    Handles: 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM:SS', 'DD.MM.YYYY HH:MM', 'YYYY-MM-DD'.
    Returns None if unparseable.
    """
    if not date_str:
        return None
    formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d"]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str[:19], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    delta = (utcnow().date() - dt.date()).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    if 2 <= delta <= 4:
        return f"{delta} дня назад"
    if delta < 7:
        return f"{delta} дней назад"
    if delta < 30:
        weeks = delta // 7
        return f"{weeks} нед. назад"
    months = delta // 30
    return f"{months} мес. назад"


def _now_storage() -> str:
    return to_storage(utcnow())


async def _send_topic_message(bot: Bot, topic_id: int, text: str, reply_markup=None) -> None:
    await bot.send_message(
        chat_id=config.group_chat_id,
        message_thread_id=topic_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )


async def _create_topic(bot: Bot, payload: dict) -> int:
    forum_topic = await bot.create_forum_topic(
        chat_id=config.group_chat_id,
        name=_build_topic_name(payload),
        icon_color=_priority_color(_payload_value(payload, "priority")),
    )
    return forum_topic.message_thread_id


async def _rename_topic_if_needed(
    bot: Bot,
    record: db.TicketTopic,
    payload: dict,
    *,
    announce: bool = False,
) -> None:
    target_name = _build_topic_name(payload)
    current_name = make_topic_name(
        record.unique_id,
        record.company_name,
        record.ticket_name,
        record.priority or "medium",
    )

    if target_name == current_name:
        return

    try:
        await bot.edit_forum_topic(
            chat_id=config.group_chat_id,
            message_thread_id=record.topic_id,
            name=target_name,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to rename topic %d for ticket %s: %s", record.topic_id, record.ticket_id, exc)
        return

    if announce and record.ticket_name != _payload_value(payload, "ticket_name"):
        try:
            await _send_topic_message(
                bot,
                record.topic_id,
                format_ticket_renamed(record.ticket_name, _payload_value(payload, "ticket_name")),
            )
        except TelegramAPIError as exc:
            logger.error("Failed to announce rename for topic %d: %s", record.topic_id, exc)


async def _generate_summary_with_retry(
    posts,
    info,
    *,
    ticket_title: str = "",
    ticket_id: str = "",
    company_id: str = "",
    topic_id: int | None = None,
    attempts: int = 3,
    pause: float = 30.0,
    trigger_source: str = "first",
):
    """Call generate_ticket_summary up to *attempts* times with *pause* seconds between tries."""
    agent_allowed = config.agent_enabled and (
        config.agent_auto_first_suggestion_enabled or trigger_source != "first"
    )
    if agent_allowed:
        try:
            result = await run_agent(
                posts, info, ticket_title=ticket_title, ticket_id=ticket_id,
                topic_id=topic_id, company_id=company_id,
                trigger_source=trigger_source,
            )
            if result is not None:
                return result
        except Exception as exc:
            logger.warning("run_agent failed, falling back to summary: %s", exc)
    for attempt in range(1, attempts + 1):
        result = await generate_ticket_summary(
            posts, info,
            ticket_title=ticket_title,
            ticket_id=ticket_id,
            company_id=company_id,
        )
        if result is not None:
            return result
        if attempt < attempts:
            logger.info(
                "AI summary attempt %d/%d failed for ticket %s, retrying in %.0fs",
                attempt, attempts, ticket_id, pause,
            )
            await asyncio.sleep(pause)
    logger.warning("AI summary failed after %d attempts for ticket %s", attempts, ticket_id)
    return None


async def _ensure_active_topic(
    bot: Bot,
    payload: dict,
    *,
    announce_assignment: bool = False,
) -> db.TicketTopic:
    ticket_id = _payload_value(payload, "ticket_id")
    record = await db.get_topic(ticket_id)
    metadata = _topic_metadata(payload)
    should_announce_assignment = False
    reassignment = False

    if record is None or record.is_deleted:
        topic_id = await _create_topic(bot, payload)
        await db.upsert_topic(ticket_id, topic_id, topic_state="active", **metadata)
        await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
        # Send assignment notification FIRST (before summary and history)
        if announce_assignment:
            notif_text = format_assignment_message(
                display_id=ticket_id,
                company_name=metadata["company_name"],
                ticket_name=metadata["ticket_name"],
                status=metadata["status"],
                priority=metadata["priority"],
                link=metadata["hde_link"],
                is_reassignment=False,
            )
            try:
                await _send_topic_message(bot, topic_id, notif_text)
            except TelegramAPIError as exc:
                logger.error("Failed to send assignment message to topic %d: %s", topic_id, exc)
        await _post_ticket_history(
            bot, ticket_id, topic_id,
            ticket_title=_payload_value(payload, "ticket_name"),
            company_id=_payload_value(payload, "company_id"),
        )
        should_announce_assignment = False  # already sent above
    elif record.is_pending_delete:
        try:
            await bot.reopen_forum_topic(
                chat_id=config.group_chat_id,
                message_thread_id=record.topic_id,
            )
        except TelegramAPIError as exc:
            if any(k in str(exc).lower() for k in ("thread not found", "not found", "deleted")):
                logger.warning(
                    "Topic %d for ticket %s not found in Telegram (pending_delete), recreating",
                    record.topic_id, ticket_id,
                )
                await db.mark_topic_deleted(ticket_id)
                return await _ensure_active_topic(bot, payload, announce_assignment=announce_assignment)
            logger.error("Failed to reopen topic %d for ticket %s: %s", record.topic_id, ticket_id, exc)
        await _rename_topic_if_needed(bot, record, payload)
        await db.upsert_topic(ticket_id, record.topic_id, topic_state="active", delete_after_at=None, deleted_at=None, **metadata)
        await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
        should_announce_assignment = announce_assignment
        reassignment = True
    else:
        # On reassignment: verify the topic still exists in Telegram.
        # Users can manually delete topics — Telegram doesn't notify the bot,
        # so DB stays "active". We probe via edit_forum_topic which throws
        # "thread not found" if the topic no longer exists.
        if announce_assignment:
            try:
                await bot.edit_forum_topic(
                    chat_id=config.group_chat_id,
                    message_thread_id=record.topic_id,
                    name=_build_topic_name(payload),
                )
            except TelegramAPIError as exc:
                err_lower = str(exc).lower()
                if "topic_not_modified" in err_lower:
                    # Same name — Telegram rejects the edit but topic is alive. Not an error.
                    pass
                elif any(k in err_lower for k in ("topic_id_invalid", "thread not found", "not found", "deleted")):
                    logger.warning(
                        "Topic %d for ticket %s no longer exists in Telegram, recreating",
                        record.topic_id, ticket_id,
                    )
                    await db.mark_topic_deleted(ticket_id)
                    return await _ensure_active_topic(bot, payload, announce_assignment=announce_assignment)
                else:
                    logger.error("Failed to verify topic %d: %s", record.topic_id, exc)
        else:
            await _rename_topic_if_needed(bot, record, payload)
        await db.update_topic(
            ticket_id,
            unique_id=metadata["unique_id"],
            company_name=metadata["company_name"],
            ticket_name=metadata["ticket_name"],
            priority=metadata["priority"],
            status=metadata["status"],
            owner_id=metadata["owner_id"],
            owner_name=metadata["owner_name"],
            hde_link=metadata["hde_link"],
            topic_state="active",
            delete_after_at=None,
            deleted_at=None,
        )
        await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
        # No should_announce_assignment here — avoids duplicates when HDE
        # fires the same owner_changed webhook twice

    record = await db.get_topic(ticket_id)
    if record is None:
        raise RuntimeError(f"Topic record for ticket {ticket_id} was not created")

    if should_announce_assignment:
        text = format_assignment_message(
            display_id=record.ticket_id,
            company_name=record.company_name,
            ticket_name=record.ticket_name,
            status=record.status,
            priority=record.priority,
            link=record.hde_link,
            is_reassignment=reassignment,
        )
        try:
            await _send_topic_message(bot, record.topic_id, text)
        except TelegramAPIError as exc:
            err = str(exc).lower()
            if "thread not found" in err or "topic_deleted" in err or "not found" in err:
                # Topic was manually deleted in Telegram — recreate it
                logger.warning(
                    "Topic %d for ticket %s not found in Telegram, recreating",
                    record.topic_id, ticket_id,
                )
                await db.mark_topic_deleted(ticket_id)
                new_topic_id = await _create_topic(bot, payload)
                await db.upsert_topic(ticket_id, new_topic_id, topic_state="active", **metadata)
                await db.update_topic(ticket_id, last_assigned_at=to_storage(utcnow()))
                try:
                    await _send_topic_message(bot, new_topic_id, text)
                except TelegramAPIError as exc2:
                    logger.error("Failed to send to recreated topic %d: %s", new_topic_id, exc2)
                await _post_ticket_history(
                    bot, ticket_id, new_topic_id,
                    ticket_title=record.ticket_name,
                )
                record = await db.get_topic(ticket_id)
            else:
                logger.error("Failed to send assignment message to topic %d: %s", record.topic_id, exc)

    return record


async def _delete_topic_now(bot: Bot, record: db.TicketTopic) -> bool:
    try:
        await bot.delete_forum_topic(
            chat_id=config.group_chat_id,
            message_thread_id=record.topic_id,
        )
    except TelegramAPIError as exc:
        err = str(exc).lower()
        if any(k in err for k in ("topic_id_invalid", "thread not found", "topic_deleted", "not found")):
            await db.mark_topic_deleted(record.ticket_id)
            logger.info(
                "Topic %d for ticket %s already gone in Telegram, marked deleted in DB",
                record.topic_id, record.ticket_id,
            )
            return True
        logger.error("Failed to delete topic %d for ticket %s: %s", record.topic_id, record.ticket_id, exc)
        return False

    await db.mark_topic_deleted(record.ticket_id)
    logger.info("Deleted topic %d for ticket %s", record.topic_id, record.ticket_id)
    return True


async def handle_owner_changed(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    async with _ticket_lock(ticket_id):
        await _handle_owner_changed_locked(bot, payload, ticket_id)


def _is_work_time() -> bool:
    from .work_schedule import is_work_time
    return is_work_time()


async def _handle_owner_changed_locked(bot: Bot, payload: dict, ticket_id: str) -> None:
    record = await db.get_topic(ticket_id)

    # Always guard deleted topics first — delayed webhooks must not recreate them
    if record is not None and record.is_deleted:
        logger.info("Ignoring owner_changed for already-deleted ticket %s", ticket_id)
        return

    if _effective_owner_match(payload):
        # Announce assignment only during work hours
        await _ensure_active_topic(bot, payload, announce_assignment=_is_work_time())
        return

    if record is None:
        logger.info("Ignoring owner_changed for untracked ticket %s", ticket_id)
        return

    if record.is_pending_delete:
        await _try_delete_pre_sla_message(bot, record)
        await db.update_topic(
            ticket_id,
            unique_id=_display_id(payload),
            company_name=_payload_value(payload, "company_name"),
            ticket_name=_payload_value(payload, "ticket_name"),
            priority=_payload_value(payload, "priority"),
            status=_payload_value(payload, "status"),
            owner_id=_payload_value(payload, "owner_id"),
            owner_name=_payload_value(payload, "owner_name"),
            hde_link=_payload_value(payload, "link"),
            pre_sla_notify_at=None,
            pre_sla_sent_at=None,
            pre_sla_message_id=None,
        )
        logger.info("Ticket %s already pending delete; skipping duplicate close", ticket_id)
        return

    delete_after = to_storage(utcnow() + timedelta(minutes=10))
    if _is_work_time():
        try:
            await _send_topic_message(
                bot,
                record.topic_id,
                format_unassigned_message(record.unique_id, _payload_value(payload, "link")),
            )
        except TelegramAPIError as exc:
            logger.error("Failed to send unassigned message to topic %d: %s", record.topic_id, exc)

    try:
        await bot.close_forum_topic(
            chat_id=config.group_chat_id,
            message_thread_id=record.topic_id,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to close topic %d for ticket %s: %s", record.topic_id, record.ticket_id, exc)

    await db.delete_reply_draft(record.topic_id)
    await _try_delete_pre_sla_message(bot, record)
    await db.update_topic(
        ticket_id,
        unique_id=_display_id(payload),
        company_name=_payload_value(payload, "company_name"),
        ticket_name=_payload_value(payload, "ticket_name"),
        priority=_payload_value(payload, "priority"),
        status=_payload_value(payload, "status"),
        owner_id=_payload_value(payload, "owner_id"),
        owner_name=_payload_value(payload, "owner_name"),
        hde_link=_payload_value(payload, "link"),
        topic_state="pending_delete",
        delete_after_at=delete_after,
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
    )


async def handle_assigned_on_create(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    if not _effective_owner_match(payload):
        logger.info("Ignoring assigned_on_create for ticket %s because owner does not match target user", ticket_id)
        return

    async with _ticket_lock(ticket_id):
        await _ensure_active_topic(bot, payload, announce_assignment=_is_work_time())


async def handle_ticket_updated(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    record = await db.get_topic(ticket_id)
    if record is None or record.is_deleted:
        logger.info("Ignoring ticket_updated for unknown ticket %s", ticket_id)
        return

    await _rename_topic_if_needed(bot, record, payload, announce=True)
    await db.update_topic(
        ticket_id,
        unique_id=_display_id(payload),
        company_name=_payload_value(payload, "company_name"),
        ticket_name=_payload_value(payload, "ticket_name"),
        priority=_payload_value(payload, "priority"),
        status=_payload_value(payload, "status"),
        owner_id=_payload_value(payload, "owner_id"),
        owner_name=_payload_value(payload, "owner_name"),
        hde_link=_payload_value(payload, "link"),
    )


async def handle_client_reply(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    async with _ticket_lock(ticket_id):
        await _handle_client_reply_locked(bot, payload, ticket_id)


async def _handle_client_reply_locked(bot: Bot, payload: dict, ticket_id: str) -> None:
    if not _is_work_time():
        return

    _existing = await db.get_topic(ticket_id)
    if _existing is not None and _existing.is_deleted:
        logger.info("Ignoring client_reply for deleted ticket %s", ticket_id)
        return

    record = await _ensure_active_topic(bot, payload)
    reply_text = format_client_reply(
        user_name=_payload_value(payload, "user_name"),
        message=_payload_value(payload, "message"),
        sla_remaining=payload.get("sla_remaining_minutes"),
        link=record.hde_link or _payload_value(payload, "link"),
        date_str=_payload_value(payload, "last_post_date"),
    )
    from .handlers.ai_feedback import suggest_button_kb
    try:
        await _send_topic_message(bot, record.topic_id, reply_text, reply_markup=suggest_button_kb())
    except TelegramAPIError as exc:
        err_lower = str(exc).lower()
        if any(k in err_lower for k in ("thread not found", "topic_id_invalid", "topic_deleted", "not found")):
            logger.warning(
                "Topic %d for ticket %s missing in Telegram on client_reply, recreating",
                record.topic_id, ticket_id,
            )
            await db.mark_topic_deleted(ticket_id)
            record = await _ensure_active_topic(bot, payload, announce_assignment=True)
            try:
                await _send_topic_message(bot, record.topic_id, reply_text, reply_markup=suggest_button_kb())
            except TelegramAPIError as exc2:
                logger.error("Failed to resend client reply to recreated topic %d: %s", record.topic_id, exc2)
        else:
            logger.error("Failed to send client reply to topic %d: %s", record.topic_id, exc)

    try:
        await _send_client_attachments(bot, record.topic_id, payload)
    except TelegramAPIError as exc:
        logger.error("Failed to send client attachments to topic %d: %s", record.topic_id, exc)

    reply_at = parse_datetime(payload.get("last_post_date")) or utcnow()
    # Only start the SLA timer on the FIRST unanswered client message.
    # Subsequent client messages don't reset the deadline — only a staff
    # reply resets it (handle_staff_reply clears pre_sla_notify_at).
    timer_active = record.pre_sla_notify_at is not None and record.pre_sla_sent_at is None
    logger.info(
        "PRESLA-DIAG client_reply ticket=%s notify_at=%r sent_at=%r timer_active=%s "
        "branch=%s reply_at=%s",
        record.ticket_id, record.pre_sla_notify_at, record.pre_sla_sent_at,
        timer_active, "else-update-lcr" if timer_active else "schedule",
        to_storage(reply_at),
    )
    if not timer_active:
        await _schedule_pre_sla(record.ticket_id, payload, to_storage(reply_at))
    else:
        # Still update last_client_reply_at so we track the latest message time
        await db.update_topic(record.ticket_id, last_client_reply_at=to_storage(reply_at))

    # Реклассификация «Окружения»: прошлая попытка дала «не определено» —
    # новое сообщение клиента может содержать недостающий контекст.
    if record.env_option_id == "":
        from .ticket_fields import retry_env_classification
        asyncio.create_task(retry_env_classification(bot, record.ticket_id, record.topic_id))


async def _maybe_update_pattern(title: str, staff_text: str, ticket_id: str) -> None:
    """Strengthen existing pattern or create new one from high-quality operator reply."""
    from .ai_summary import _detect_equipment
    from . import db as _db3
    import difflib

    equipment = _detect_equipment(title, staff_text)

    # Check if similar pattern exists → increment use_count
    patterns = await _db3.list_solution_patterns(limit=100)
    for p in patterns:
        if p["equipment"] != equipment:
            continue
        ratio = difflib.SequenceMatcher(
            None, p["problem_type"].lower(), title.lower()
        ).ratio()
        if ratio >= 0.7:
            await _db3.increment_pattern_use(p["id"])
            logger.debug(
                "Pattern %d reinforced for ticket %s (ratio=%.2f)",
                p["id"], ticket_id, ratio,
            )
            return

    # No existing pattern — extract new one via Groq (non-fatal)
    from . import ai_summary as _ai

    prompt = (
        "Из ответа технического специалиста извлеки:\n"
        "- problem_type: тип проблемы (5-10 слов)\n"
        "- steps: шаги решения через →\n"
        "Верни JSON: {\"problem_type\": ..., \"steps\": ...}\n"
        "Только JSON. Если шагов нет — верни {}.\n\n"
        f"Тема: {title}\nОтвет специалиста: {staff_text[:600]}"
    )
    try:
        raw = await _ai.call_groq_text(prompt, max_tokens=300, temperature=0.1)
        if raw is None:
            return
        import re as _re, json as _json
        raw = _re.sub(r"^```[^\n]*\n?", "", raw).rstrip("`").strip()
        parsed = _json.loads(raw)
        pt = (parsed.get("problem_type") or "").strip()
        st = (parsed.get("steps") or "").strip()
        if pt and st and not await _db3.pattern_exists_similar(equipment, pt):
            await _db3.save_solution_pattern(
                problem_type=pt, steps=st, source="implicit", equipment=equipment
            )
            logger.info(
                "New implicit pattern created for ticket %s: %r", ticket_id, pt
            )
    except Exception as exc:
        logger.warning("_maybe_update_pattern Groq call failed: %s", exc)


async def handle_staff_reply(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    async with _ticket_lock(ticket_id):
        await _handle_staff_reply_locked(bot, payload, ticket_id)


async def _handle_staff_reply_locked(bot: Bot, payload: dict, ticket_id: str) -> None:
    record = await db.get_topic(ticket_id)
    if record is None or record.is_deleted:
        logger.info("Ignoring staff_reply for unknown ticket %s", ticket_id)
        return

    reply_at = parse_datetime(payload.get("last_post_date")) or utcnow()
    # Clear the pre-SLA timer only when this staff reply is genuinely newer
    # than the last client message. A staff_reply at/before the last client
    # reply (duplicate, out-of-order, or bot/HDE echo) must NOT wipe a timer
    # that belongs to a still-unanswered client message.
    last_client = (
        parse_datetime(record.last_client_reply_at)
        if record.last_client_reply_at else None
    )
    # Floor to whole seconds before comparing: HDE dispatcher echoes
    # (and the utcnow() fallback when payload last_post_date is empty)
    # produce sub-second drift past a second-precision stored
    # last_client_reply_at — without flooring, a same-second staff event
    # spuriously clears the just-armed pre-SLA timer.
    reply_at_sec = reply_at.replace(microsecond=0)
    last_client_sec = (
        last_client.replace(microsecond=0) if last_client else None
    )
    newer_than_client = last_client_sec is None or reply_at_sec > last_client_sec
    if not newer_than_client:
        # stale / out-of-order / at-or-before the client message — never clears
        should_clear = False
    elif config.presla_hde_verify and config.hde_owner_id.strip():
        # Only the operator's own HDE post should clear the timer. Automated
        # replies (PosifloraSupportBot, dispatcher echoes) fire staff_reply too
        # but have a different HDE user_id — verify the operator genuinely
        # replied after the client before wiping the pre-SLA timer.
        should_clear = await _hde_staff_replied_since(
            ticket_id, record.last_client_reply_at
        )
    else:
        # No HDE verification available — trust the staff_reply event (legacy).
        should_clear = True
    logger.info(
        "PRESLA-DIAG staff_reply ticket=%s should_clear=%s had_notify_at=%r "
        "had_sent_at=%r staff_reply_at=%s last_client_reply_at=%r",
        ticket_id, should_clear, record.pre_sla_notify_at, record.pre_sla_sent_at,
        to_storage(reply_at), record.last_client_reply_at,
    )
    if should_clear:
        await _try_delete_pre_sla_message(bot, record)
    pre_sla_clear = (
        {"pre_sla_notify_at": None, "pre_sla_sent_at": None, "pre_sla_message_id": None}
        if should_clear else {}
    )
    await db.update_topic(
        ticket_id,
        unique_id=_display_id(payload),
        company_name=_payload_value(payload, "company_name"),
        ticket_name=_payload_value(payload, "ticket_name"),
        priority=_payload_value(payload, "priority"),
        status=_payload_value(payload, "status"),
        owner_id=_payload_value(payload, "owner_id"),
        owner_name=_payload_value(payload, "owner_name"),
        hde_link=_payload_value(payload, "link"),
        last_staff_reply_at=to_storage(reply_at),
        **pre_sla_clear,
    )

    # Implicit feedback: compare AI suggestion with what operator actually sent
    staff_text = _payload_value(payload, "message") or ""
    if staff_text and record:
        try:
            await _implicit_feedback(record, staff_text)
        except Exception as exc:
            logger.warning("Implicit feedback failed for ticket %s: %s", ticket_id, exc)


async def handle_ticket_closed(bot: Bot, payload: dict) -> None:
    ticket_id = _payload_value(payload, "ticket_id")
    async with _ticket_lock(ticket_id):
        await _handle_ticket_closed_locked(bot, payload, ticket_id)


async def _handle_ticket_closed_locked(bot: Bot, payload: dict, ticket_id: str) -> None:
    record = await db.get_topic(ticket_id)
    if record is None or record.is_deleted:
        logger.info("Ignoring ticket_closed for unknown ticket %s", ticket_id)
        return
    if record.is_pending_delete:
        logger.info("Ticket %s already pending delete, skipping duplicate ticket_closed", ticket_id)
        return

    await _try_delete_pre_sla_message(bot, record)
    await db.update_topic(
        ticket_id,
        unique_id=_display_id(payload),
        company_name=_payload_value(payload, "company_name"),
        ticket_name=_payload_value(payload, "ticket_name"),
        priority=_payload_value(payload, "priority"),
        status=_payload_value(payload, "status"),
        owner_id=_payload_value(payload, "owner_id"),
        owner_name=_payload_value(payload, "owner_name"),
        hde_link=_payload_value(payload, "link"),
        pre_sla_notify_at=None,
        pre_sla_sent_at=None,
        pre_sla_message_id=None,
    )

    # Сверка прогноза «Окружения» с финальным значением (оператор мог поправить)
    if record.env_option_id is not None:
        from .ticket_fields import log_env_outcome
        await log_env_outcome(ticket_id, record.env_option_id or None)

    if record.priority_option_id is not None:
        from .ticket_fields import log_pt_outcome
        await log_pt_outcome(
            ticket_id,
            record.priority_option_id or None,
            record.type_option_id or None,
        )

    ok = await _delete_topic_now(bot, record)
    if ok:
        logger.info("Ticket %s completed, topic %d deleted immediately", ticket_id, record.topic_id)
    else:
        logger.warning(
            "Ticket %s completed but topic %d could not be deleted (will retry via reconcile)",
            ticket_id, record.topic_id,
        )


async def _hde_staff_replied_since(ticket_id: str, since_storage: Optional[str]) -> bool:
    """Return True if operator posted in HDE after last client reply.

    Fails open (returns False) on any error so pre-SLA is never silently swallowed.
    Skips check when presla_hde_verify=False or hde_owner_id not configured.
    """
    if not config.presla_hde_verify:
        return False
    owner_id_str = config.hde_owner_id.strip()
    if not owner_id_str:
        return False
    try:
        owner_id = int(owner_id_str)
    except ValueError:
        return False

    since_dt = parse_datetime(since_storage)

    try:
        from .hde_api import HDEApiClient
        client = HDEApiClient()
        posts = await client.get_ticket_posts(ticket_id, limit=5)
        # get_ticket_posts returns oldest-first; iterate newest-first
        for post in reversed(posts):
            if post.user_id != owner_id:
                continue
            if since_dt is None:
                return True
            try:
                from datetime import timezone, timedelta
                _msk = timezone(timedelta(hours=3))
                post_dt = datetime.strptime(
                    post.date_created, "%H:%M:%S %d.%m.%Y"
                ).replace(tzinfo=_msk).astimezone(timezone.utc)
            except (ValueError, AttributeError):
                continue
            if post_dt > since_dt:
                return True
        return False
    except Exception as exc:
        logger.warning("pre-SLA HDE verify failed for ticket %s: %s", ticket_id, exc)
        return False


async def delete_pending_topic(bot: Bot, record: db.TicketTopic) -> bool:
    return await _delete_topic_now(bot, record)


async def sync_ticket_topic(bot: Bot, payload: dict) -> db.TicketTopic:
    """Upsert a topic for one ticket without sending an assignment announcement.

    Used by /refresh to bring Telegram topics in sync with HDE state.
    Serialised per-ticket via _ticket_lock to avoid race conditions.
    """
    ticket_id = _payload_value(payload, "ticket_id")
    async with _ticket_lock(ticket_id):
        return await _ensure_active_topic(bot, payload, announce_assignment=False)


async def _post_client_history(
    bot: Bot,
    topic_id: int,
    ticket_id: str,
    *,
    client=None,
    info=None,
) -> None:
    """Fetch client's past tickets from HDE and post a summary to the topic.

    Silently skips on any error or if no past tickets exist.
    """
    try:
        from .hde_api import HDEApiClient
        if client is None:
            client = HDEApiClient()
        if info is None:
            info = await client.get_ticket_info(ticket_id)
        if not info or not info.client_id:
            return
        tickets = await client.get_client_tickets(info.client_id, limit=10)
        past = [t for t in tickets if str(t.get("id", "")) != str(ticket_id)]
        if not past:
            return
        recent_titles = [t["subject"] for t in past[:5] if t.get("subject")]
        last_date = _relative_date(past[0].get("date_created"))
        text = format_client_history(
            client_name=info.client_name,
            total=len(past),
            recent_titles=recent_titles,
            last_ticket_date=last_date,
        )
        if text:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=True,
            )
    except Exception as exc:
        logger.warning("Client history failed for ticket %s: %s", ticket_id, exc)


# ---------------------------------------------------------------------------
# Re-exports: implementation mechanically moved to satellite modules.
# Kept importable from bot.topic_manager so existing importers and tests
# (`from bot.topic_manager import X`) keep working unchanged.
# Imported at the bottom to avoid circular imports (the satellite modules
# import bot.topic_manager lazily inside their functions).
# ---------------------------------------------------------------------------
from .topic_sla import (  # noqa: E402
    _calculate_pre_sla_notify_at,
    _parse_minutes,
    _pre_sla_destination,
    _pre_sla_minutes_left,
    _pre_sla_text,
    _schedule_pre_sla,
    _try_delete_pre_sla_message,
    send_pre_sla_alert,
    send_reassurance_to_client,
    update_pre_sla_alert,
)
from .topic_history import (  # noqa: E402
    _post_ticket_history,
    retry_missing_ai_summaries,
)
from .topic_media import (  # noqa: E402
    _describe_and_post_photos,
    _send_client_attachments,
)
from .topic_learning import _implicit_feedback  # noqa: E402
