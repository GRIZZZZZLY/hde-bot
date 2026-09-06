import logging
from datetime import date, datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ..db import (
    count_active_topics,
    count_pending_delete_topics,
    count_pending_pre_sla_topics,
    count_total_topics,
)
from .. import db
from .. import metrics
from ..ai_summary import _build_history_text
from ..digest import send_morning_digest
from ..formatter import format_refresh_result
from ..hde_api import HDEApiClient, HDEApiError
from ..refresh import refresh_topics
from ..ticket_fields import (
    FIELD_ROL,
    OKRUZHENIE_OPTIONS,
    AutofillResult,
    apply_ticket_fields,
)
from ..command_menu import hub_keyboard, submenu_keyboard, back_keyboard, HUB_TITLE, ARGS_HELP_TEXT
from ..operator_replies import (
    OperatorReplyError,
    add_internal_note,
    delete_operator_message,
    edit_operator_message,
    format_operator_exception,
    get_operator_topic_context,
    send_public_reply,
)

logger = logging.getLogger(__name__)
router = Router()


async def _run_operator_command(
    message: Message,
    action,
) -> None:
    try:
        result = await action()
    except Exception as exc:
        await message.answer(format_operator_exception(exc), parse_mode="HTML")
        return

    await message.answer(result, parse_mode="HTML", disable_web_page_preview=True)



def _format_metrics_block() -> str:
    """Ops counters for /status. Reset on restart, so uptime frames them."""
    snap = metrics.snapshot()
    uptime_min = snap.get("uptime_seconds", 0) // 60
    lines = [f"\n\n⚙️ <b>Метрики</b> (за {uptime_min} мин аптайма)"]
    lines.append(
        f"📨 Вебхуки: <b>{snap.get('webhook_received', 0)}</b> получено · "
        f"{snap.get('webhook_processed', 0)} обработано · "
        f"{snap.get('webhook_duplicate', 0)} дублей · "
        f"{snap.get('webhook_failed', 0)} ошибок"
    )
    llm_calls = snap.get("llm_calls", 0)
    llm_line = f"🧠 LLM: <b>{llm_calls}</b> вызовов · {snap.get('llm_failures', 0)} ошибок"
    if "llm_latency_avg_ms" in snap:
        llm_line += f" · ~{snap['llm_latency_avg_ms']} мс"
    lines.append(llm_line)
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    active_topics = await count_active_topics()
    pending_delete = await count_pending_delete_topics()
    pending_pre_sla = await count_pending_pre_sla_topics()
    total_topics = await count_total_topics()

    await message.answer(
        "📊 <b>Статус бота</b>\n\n"
        f"🟢 Активных topics: <b>{active_topics}</b>\n"
        f"⏳ Ожидают удаления: <b>{pending_delete}</b>\n"
        f"⏰ Ожидают pre-SLA: <b>{pending_pre_sla}</b>\n"
        f"📚 Всего записей: <b>{total_topics}</b>"
        + _format_metrics_block(),
        parse_mode="HTML",
    )


@router.message(Command("help"))
@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(HUB_TITLE, parse_mode="HTML", reply_markup=hub_keyboard())


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        return await add_internal_note(
            bot=message.bot,
            context=context,
            message=message,
            command_args=command.args,
            tg_message_id=message.message_id,
        )

    await _run_operator_command(message, action)


def _format_autofill_result(r: AutofillResult) -> str:
    if not r.updated:
        return f"⚠️ <b>Автозаполнение не выполнено:</b> {r.error or 'неизвестная ошибка'}"
    lines = ["✅ <b>Поля тикета обновлены</b>", "• Классификация: Оборудование"]
    if r.env_id:
        lines.append(f"• Окружение: {OKRUZHENIE_OPTIONS.get(r.env_id, r.env_id)}")
    else:
        lines.append("• Окружение: ⚠️ не определено — выставьте вручную")
    if FIELD_ROL in r.fields:
        lines.append("• Роль: Не важно (поле было пустым)")
    else:
        lines.append("• Роль: без изменений")
    return "\n".join(lines)


@router.message(Command("autofill"))
async def cmd_autofill(message: Message) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        ticket_id = context.record.ticket_id
        client = HDEApiClient()
        info = await client.get_ticket_info(ticket_id)
        posts = await client.get_ticket_posts(ticket_id)
        try:
            comments = await client.get_ticket_comments(ticket_id)
        except HDEApiError:
            comments = []
        all_posts = sorted(posts + comments, key=lambda p: p.date_created)
        history = _build_history_text(all_posts, info)
        result = await apply_ticket_fields(
            message.bot, ticket_id, context.topic_id, history,
            ticket_title=context.record.ticket_name or "",
            posts=all_posts,
        )
        return _format_autofill_result(result)

    await _run_operator_command(message, action)


@router.message(Command("send"))
async def cmd_send(message: Message, command: CommandObject) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        return await send_public_reply(
            bot=message.bot,
            context=context,
            message=message,
            command_args=command.args,
            tg_message_id=message.message_id,
        )

    await _run_operator_command(message, action)


@router.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        reply = message.reply_to_message
        if reply is None:
            raise OperatorReplyError("Ответьте командой /delete на сообщение, которое хотите удалить из HDE")
        return await delete_operator_message(
            context=context,
            telegram_message_id=reply.message_id,
        )

    await _run_operator_command(message, action)


@router.callback_query(F.data.startswith("take:"))
async def cb_take_ticket(callback: CallbackQuery) -> None:
    """Inline button: assign unassigned ticket to me."""
    from ..hde_api import HDEApiClient, HDEApiError
    from ..config import config
    from .. import db

    ticket_id = callback.data.split(":", 1)[1]

    # Prevent double-tap: remove button immediately
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("take_ticket: reply markup cleanup failed: %s", exc)

    if not config.hde_owner_id:
        await callback.answer("HDE_OWNER_ID не задан", show_alert=True)
        return

    try:
        api = HDEApiClient()
        await api.assign_ticket(ticket_id, config.hde_owner_id)
    except HDEApiError as exc:
        await callback.answer(f"Ошибка HDE: {exc}", show_alert=True)
        # Restore button on failure
        from ..general_channel import _take_keyboard
        try:
            await callback.message.edit_reply_markup(reply_markup=_take_keyboard(ticket_id))
        except Exception as restore_exc:
            logger.debug("take_ticket: button restore failed: %s", restore_exc)
        return

    owner_name = config.hde_owner_name or "Оператор"
    try:
        await callback.message.edit_text(
            f"✅ <b>Забрал {owner_name}</b>\n\n{callback.message.text or ''}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.debug("take_ticket: message edit failed: %s", exc)

    # Remove DB record so on_owner_changed webhook skips deletion
    await db.delete_general_message(ticket_id)
    await callback.answer(f"Тикет {ticket_id} назначен на тебя")


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery) -> None:
    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "root":
        await callback.message.edit_text(
            HUB_TITLE, parse_mode="HTML", reply_markup=hub_keyboard()
        )
        return
    if action in ("quiet", "reports", "ai"):
        await callback.message.edit_text(
            HUB_TITLE, parse_mode="HTML", reply_markup=submenu_keyboard(action)
        )
        return
    if action == "args":
        await callback.message.edit_text(
            ARGS_HELP_TEXT, parse_mode="HTML", reply_markup=back_keyboard()
        )
        return

    if action == "status":
        active_topics = await count_active_topics()
        pending_delete = await count_pending_delete_topics()
        pending_pre_sla = await count_pending_pre_sla_topics()
        total_topics = await count_total_topics()
        await callback.message.answer(
            "📊 <b>Статус бота</b>\n\n"
            f"🟢 Активных topics: <b>{active_topics}</b>\n"
            f"⏳ Ожидают удаления: <b>{pending_delete}</b>\n"
            f"⏰ Ожидают pre-SLA: <b>{pending_pre_sla}</b>\n"
            f"📚 Всего записей: <b>{total_topics}</b>",
            parse_mode="HTML",
        )
        return
    if action == "refresh":
        wait_msg = await callback.message.answer("🔄 Синхронизирую с HDE...")
        error_text: str | None = None
        result_text: str | None = None
        try:
            result = await refresh_topics(bot=callback.bot)
            result_text = format_refresh_result(
                active_count=result.active_after,
                hde_count=result.hde_count,
                created=result.created,
                renamed=result.renamed,
                deleted=result.deleted,
                cleaned_pending=result.cleaned_pending,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("cb_menu refresh failed")
            error_text = f"⚠️ <b>Ошибка при синхронизации:</b> {exc}"
        try:
            await wait_msg.delete()
        except Exception as exc:
            logger.debug("cb_menu: wait message cleanup failed: %s", exc)
        await callback.message.answer(error_text or result_text, parse_mode="HTML")
        from ..topic_manager import retry_missing_ai_summaries
        try:
            sent = await retry_missing_ai_summaries(callback.bot)
            if sent:
                await callback.message.answer(
                    f"🧠 <b>AI саммари отправлено:</b> {sent} топик(ов)",
                    parse_mode="HTML",
                )
        except Exception as exc:
            logger.warning("retry_missing_ai_summaries failed: %s", exc)
        return
    if action == "report_yesterday":
        from .report_commands import cb_report_yesterday
        await cb_report_yesterday(callback)
        return
    if action == "digest":
        try:
            await send_morning_digest(callback.bot)
            await callback.message.answer("🌅 Утренняя сводка отправлена.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("cb_menu digest failed")
            await callback.message.answer(f"⚠️ <b>Ошибка:</b> {exc}", parse_mode="HTML")
        return
    if action == "vacation":
        from ..work_schedule import enable_vacation, next_work_start
        import zoneinfo
        _MSK = zoneinfo.ZoneInfo("Europe/Moscow")
        until = next_work_start()
        await enable_vacation(until)
        until_msk = until.astimezone(_MSK)
        await callback.message.answer(
            f"🏖 <b>Режим отпуска включён</b>\n"
            f"Уведомления возобновятся: <b>{until_msk.strftime('%d.%m.%Y %H:%M')} МСК</b>",
            parse_mode="HTML",
        )
        return
    if action == "workon":
        from ..work_schedule import disable_vacation, is_on_vacation
        if not is_on_vacation():
            await callback.message.answer("ℹ️ Режим отпуска не активен.")
            return
        await disable_vacation()
        await callback.message.answer(
            "✅ <b>Режим отпуска отключён.</b> Уведомления возобновлены.", parse_mode="HTML"
        )
        return
    if action == "aisummary":
        from ..db import get_setting, set_setting
        current = await get_setting("ai_summary_enabled", "1")
        new = "0" if current == "1" else "1"
        await set_setting("ai_summary_enabled", new)
        if new == "1":
            await callback.message.answer("🧠 <b>AI Саммари включён</b> ✅", parse_mode="HTML")
        else:
            await callback.message.answer("🧠 <b>AI Саммари выключен</b> ❌", parse_mode="HTML")
        return
    if action == "aimetrics":
        from .ai_commands import cmd_aimetrics
        await cmd_aimetrics(callback.message)
        return
    logger.warning("cb_menu: unknown action %r", action)


@router.message(Command("vacation"))
async def cmd_vacation(message: Message, command: CommandObject) -> None:
    """
    /vacation        — до следующего рабочего дня
    /vacation 3d     — на N дней
    /vacation 2026-04-15  — до конкретной даты (МСК полночь)
    """
    from ..work_schedule import enable_vacation, next_work_start
    import zoneinfo

    args = (command.args or "").strip()
    now_utc = datetime.now(timezone.utc)
    _MSK = zoneinfo.ZoneInfo("Europe/Moscow")

    until: datetime
    if not args:
        until = next_work_start()
    elif args.endswith("d") and args[:-1].isdigit():
        days = int(args[:-1])
        until = now_utc + timedelta(days=days)
    else:
        try:
            d = date.fromisoformat(args)
            # Until the start of that day in MSK
            until = datetime(d.year, d.month, d.day, 9, 0, tzinfo=_MSK).astimezone(timezone.utc)
        except ValueError:
            await message.answer(
                "⚠️ Неверный формат. Примеры:\n"
                "/vacation — до следующего рабочего дня\n"
                "/vacation 3d — на 3 дня\n"
                "/vacation 2026-04-15 — до 15 апреля",
                parse_mode="HTML",
            )
            return

    await enable_vacation(until)
    until_msk = until.astimezone(_MSK)
    await message.answer(
        f"🏖 <b>Режим отпуска включён</b>\n"
        f"Уведомления возобновятся: <b>{until_msk.strftime('%d.%m.%Y %H:%M')} МСК</b>",
        parse_mode="HTML",
    )


@router.message(Command("workon"))
async def cmd_workon(message: Message) -> None:
    """Снять режим отпуска досрочно."""
    from ..work_schedule import disable_vacation, is_on_vacation

    if not is_on_vacation():
        await message.answer("ℹ️ Режим отпуска не активен.")
        return

    await disable_vacation()
    await message.answer("✅ <b>Режим отпуска отключён.</b> Уведомления возобновлены.", parse_mode="HTML")


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    await send_morning_digest(message.bot)


@router.message(Command("refresh"))
async def cmd_refresh(message: Message) -> None:
    wait_msg = await message.answer("🔄 Синхронизирую с HDE...")
    error_text: str | None = None
    result_text: str | None = None
    try:
        result = await refresh_topics(bot=message.bot)
        result_text = format_refresh_result(
            active_count=result.active_after,
            hde_count=result.hde_count,
            created=result.created,
            renamed=result.renamed,
            deleted=result.deleted,
            cleaned_pending=result.cleaned_pending,
        )
    except Exception as exc:
        error_text = f"⚠️ <b>Ошибка при синхронизации:</b> {exc}"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("refresh: wait message cleanup failed: %s", exc)
    await message.answer(error_text or result_text, parse_mode="HTML")

    # Retry AI summaries for topics that missed them
    from ..topic_manager import retry_missing_ai_summaries
    try:
        sent = await retry_missing_ai_summaries(message.bot)
        if sent:
            await message.answer(
                f"🧠 <b>AI саммари отправлено:</b> {sent} топик(ов)",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("retry_missing_ai_summaries failed: %s", exc)


@router.edited_message(F.message_thread_id.is_not(None))
async def on_edited_message(message: Message) -> None:
    """When operator edits a message in a topic, sync the edit to HDE."""
    if message.from_user is None or message.from_user.is_bot:
        return

    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        new_text = (message.text or message.caption or "").strip()
        return await edit_operator_message(
            context=context,
            telegram_message_id=message.message_id,
            new_text=new_text,
        )

    try:
        result = await action()
    except OperatorReplyError:
        # Silently ignore — most edits in topics are unrelated to HDE
        return
    except Exception as exc:
        logger.debug("on_edited_message: edit sync failed: %s", exc)
        return

    await message.answer(result, parse_mode="HTML")


# --- Sub-routers, split by feature area -------------------------------------
# Own handlers above are matched first, then sub-routers in include order.
# The media router goes LAST: it contains the catch-all @router.message()
# which must not shadow the other routers' command handlers.
from .ai_commands import router as _ai_router  # noqa: E402
from .optimizer_commands import router as _optimizer_router  # noqa: E402
from .report_commands import router as _report_router  # noqa: E402
from .media_commands import router as _media_router  # noqa: E402

router.include_router(_ai_router)
router.include_router(_optimizer_router)
router.include_router(_report_router)
router.include_router(_media_router)

# Compatibility re-exports: tests and callers import these names from here.
from .ai_commands import (  # noqa: E402,F401
    cmd_aisummary,
    cmd_aiknowledge,
    cmd_aimetrics,
    cb_km_dedup,
    cb_km_expire,
    cmd_aiimport,
    cmd_aibackfill,
    cmd_aireindex,
    cmd_aianalyze,
)
from .optimizer_commands import (  # noqa: E402,F401
    cmd_aioptimize,
    cb_opt_apply,
    cb_opt_reject,
    cb_opt_detail,
    cmd_promptrollback,
    cb_rollback_apply,
)
from .report_commands import cmd_report, cmd_weekly, cb_report_cancel, cb_report_yesterday  # noqa: E402,F401
from .media_commands import handle_topic_call_recording, cache_topic_media  # noqa: E402,F401
