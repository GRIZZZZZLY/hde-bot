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
from .. import operators
from ..ai_summary import _build_history_text
from ..digest import send_morning_digest
from ..formatter import format_refresh_result
from ..hde_api import HDEApiClient, HDEApiError, post_sort_key
from ..refresh import refresh_topics
from ..ticket_fields import (
    FIELD_ROL,
    OKRUZHENIE_OPTIONS,
    AutofillResult,
    apply_ticket_fields,
)
from .. import command_menu as menu
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


async def status_text() -> str:
    active_topics = await count_active_topics()
    pending_delete = await count_pending_delete_topics()
    pending_pre_sla = await count_pending_pre_sla_topics()
    total_topics = await count_total_topics()
    return (
        "📊 <b>Статус бота</b>\n\n"
        f"🟢 Активных topics: <b>{active_topics}</b>\n"
        f"⏳ Ожидают удаления: <b>{pending_delete}</b>\n"
        f"⏰ Ожидают pre-SLA: <b>{pending_pre_sla}</b>\n"
        f"📚 Всего записей: <b>{total_topics}</b>"
        + _format_metrics_block()
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await message.answer(await status_text(), parse_mode="HTML")


@router.message(Command("start"))
@router.message(Command("help"))
@router.message(Command("menu"))
@router.message(F.text == menu.MENU_BUTTON_TEXT)
async def cmd_menu(message: Message) -> None:
    if message.chat.type == "private":
        # A reply keyboard persists once sent, so set the ☰ button once per chat.
        key = f"menu_keyboard_sent:{message.chat.id}"
        if not await db.get_setting(key):
            await message.answer("☰ Кнопка меню теперь всегда под полем ввода.",
                                 reply_markup=menu.menu_reply_keyboard())
            await db.set_setting(key, "1")
    await message.answer(menu.HUB_TITLE, parse_mode="HTML", reply_markup=menu.hub_keyboard())


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
            chat_id=message.chat.id,
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
    from ..config import config
    if not r.updated:
        return f"⚠️ <b>Автозаполнение не выполнено:</b> {r.error or 'неизвестная ошибка'}"
    lines = ["✅ <b>Поля тикета обновлены</b>", "• Классификация: Оборудование"]
    if not config.env_autofill_enabled:
        lines.append("• Окружение: без изменений (автоопределение выключено)")
    elif r.env_id:
        lines.append(f"• Окружение: {OKRUZHENIE_OPTIONS.get(r.env_id, r.env_id)}")
    else:
        lines.append("• Окружение: ⚠️ не определено — выставьте вручную")
    if FIELD_ROL in r.fields:
        lines.append("• Роль: Не важно (поле было пустым)")
    else:
        lines.append("• Роль: без изменений")
    return "\n".join(lines)


async def run_autofill(bot, *, chat_id: int, topic_id: int, tg_user_id: int) -> str:
    """Fill Окружение/Классификация/Роль for the topic's ticket; HTML result text."""
    context = await get_operator_topic_context(
        telegram_user_id=tg_user_id, topic_id=topic_id, chat_id=chat_id,
    )
    ticket_id = context.record.ticket_id
    if not operators.ai_enabled_for(chat_id=chat_id):
        return _format_autofill_result(AutofillResult(updated=False, error=operators.AI_OFF_NOTE))
    client = HDEApiClient()
    info = await client.get_ticket_info(ticket_id)
    posts = await client.get_ticket_posts(ticket_id)
    try:
        comments = await client.get_ticket_comments(ticket_id)
    except HDEApiError:
        comments = []
    all_posts = sorted(posts + comments, key=post_sort_key)
    history = _build_history_text(all_posts, info)
    result = await apply_ticket_fields(
        bot, ticket_id, chat_id, context.topic_id, history,
        ticket_title=context.record.ticket_name or "",
        posts=all_posts,
    )
    return _format_autofill_result(result)


@router.message(Command("autofill"))
async def cmd_autofill(message: Message) -> None:
    await _run_operator_command(message, lambda: run_autofill(
        message.bot, chat_id=message.chat.id,
        topic_id=message.message_thread_id, tg_user_id=message.from_user.id,
    ))


@router.message(Command("send"))
async def cmd_send(message: Message, command: CommandObject) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
            chat_id=message.chat.id,
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
            chat_id=message.chat.id,
        )
        reply = message.reply_to_message
        if reply is None:
            raise OperatorReplyError("Ответьте командой /delete на сообщение, которое хотите удалить из HDE")
        return await delete_operator_message(
            context=context,
            telegram_message_id=reply.message_id,
        )

    await _run_operator_command(message, action)


@router.message(Command("taketimes"))
async def cmd_taketimes(message: Message, command: CommandObject) -> None:
    """/taketimes — show, /taketimes 1 1-2 2 4 — set the «busy» hour buttons in General."""
    from ..config import config
    from ..general_channel import TAKE_HOURS_SETTING, busy_options, hours_label, parse_busy_options

    if not message.from_user or not config.is_operator_allowed(message.from_user.id):
        return
    args = (command.args or "").strip()
    if args:
        options = parse_busy_options(args)
        if options is None:
            await message.answer(
                "Не понял. От 1 до 6 вариантов через пробел, часы от 1 до 72, "
                "диапазон через дефис: <code>/taketimes 1 1-2 2 4</code>",
                parse_mode="HTML",
            )
            return
        await db.set_setting(TAKE_HOURS_SETTING, " ".join(options))
    current = " · ".join(f"{hours_label(o)} ч" for o in await busy_options())
    await message.answer(
        f"⏳ Кнопки под новым тикетом: <b>{current}</b>\n"
        "Новые сообщения получат их сразу, уже отправленные — нет.\n"
        "Изменить: <code>/taketimes 1 1-2 2 4</code>",
        parse_mode="HTML",
    )


async def _send_take_greeting(ticket_id: str, mode: str, operator) -> str:
    """Post the greeting to the client from the engineer who took the ticket;
    return a status suffix for the General message.

    The ticket is already assigned at this point, so a failure here is reported,
    not rolled back: the engineer writes the greeting by hand.
    """
    from ..config import config
    from ..general_channel import hours_label, take_greeting
    from ..hde_api import HDEApiClient, HDEApiError

    promise = "вернусь через 5 мин" if mode == "now" else f"вернусь в течение {hours_label(mode)} ч"
    first_name = operator.first_name
    if not config.public_reply_enabled or not first_name:
        return f"\n⚠️ Клиенту не написал (публичные ответы выключены или нет имени инженера): «{promise}»"
    try:
        await HDEApiClient(auth=operator.api_auth).add_post(ticket_id, text=take_greeting(first_name, mode))
    except HDEApiError as exc:
        logger.error("take_ticket: greeting for ticket %s failed: %s", ticket_id, exc)
        return f"\n⚠️ Клиенту не ушло ({exc}), напиши сам: «{promise}»"
    logger.info("take_ticket: greeting sent to ticket %s (mode=%s)", ticket_id, mode)
    return f"\n💬 Клиенту: «{promise}»"


# Tickets being taken right now. One bot process, so an in-memory claim is enough
# to make «first press wins» hold across the engineers' groups.
_taking: set[str] = set()


@router.callback_query(F.data.startswith("take:"))
async def cb_take_ticket(callback: CallbackQuery) -> None:
    """Inline button: assign the unassigned ticket to the engineer who pressed it."""
    from ..hde_api import HDEApiClient, HDEApiError
    from ..config import config
    from .. import db
    from .. import operators

    # take:{id} (old messages) | take:{id}:now | take:{id}:{hours}
    _, ticket_id, *rest = callback.data.split(":")
    mode = rest[0] if rest else ""
    from ..general_channel import is_busy_option

    if mode and mode != "now" and not is_busy_option(mode):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return

    operator = operators.by_tg_user(callback.from_user.id)
    if operator is None and config.is_operator_allowed(callback.from_user.id):
        operator = operators.primary()  # extra admin ids from OPERATOR_TELEGRAM_USER_IDS
    if operator is None or not operator.hde_id:
        await callback.answer("Ты не в списке инженеров бота", show_alert=True)
        return
    if ticket_id in _taking or await db.get_general_message(ticket_id) is None:
        await callback.answer("Тикет уже забрали", show_alert=True)
        return
    _taking.add(ticket_id)
    try:
        await _take(callback, ticket_id, mode, operator)
    finally:
        _taking.discard(ticket_id)


async def _take(callback: CallbackQuery, ticket_id: str, mode: str, operator) -> None:
    from ..hde_api import HDEApiClient, HDEApiError
    from .. import db

    # Prevent double-tap: remove button immediately
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("take_ticket: reply markup cleanup failed: %s", exc)

    try:
        api = HDEApiClient(auth=operator.api_auth)
        await api.assign_ticket(ticket_id, operator.hde_id)
    except HDEApiError as exc:
        await callback.answer(f"Ошибка HDE: {exc}", show_alert=True)
        # Restore button on failure
        from ..general_channel import _take_keyboard
        try:
            await callback.message.edit_reply_markup(reply_markup=await _take_keyboard(ticket_id))
        except Exception as restore_exc:
            logger.debug("take_ticket: button restore failed: %s", restore_exc)
        return

    status_line = f"✅ <b>Забрал {operator.name or 'Оператор'}</b>"
    if mode:
        status_line += await _send_take_greeting(ticket_id, mode, operator)
    try:
        await callback.message.edit_text(
            f"{status_line}\n\n{callback.message.text or ''}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.debug("take_ticket: message edit failed: %s", exc)

    # The same notification in the other engineers' groups: show who took it.
    taken_text = f"✅ <b>Забрал {operator.name or 'Оператор'}</b>\n\n{callback.message.text or ''}"
    for row in await db.list_general_messages_for(ticket_id):
        if row["chat_id"] == callback.message.chat.id:
            continue
        try:
            await callback.bot.edit_message_text(
                chat_id=row["chat_id"], message_id=row["message_id"], text=taken_text,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.debug("take_ticket: could not mark taken in chat %s: %s", row["chat_id"], exc)

    # Remove DB records so on_owner_changed webhook skips deletion
    await db.delete_general_message(ticket_id)
    await callback.answer(f"Тикет {ticket_id} назначен на тебя")


async def menu_screen(name: str):
    """(text, keyboard) for a menu screen, or None if `name` is not a screen."""
    from ..config import config
    from ..general_channel import busy_options
    from ..work_schedule import _today_msk, is_on_vacation, vacation_until
    import zoneinfo

    if name == "root":
        return menu.HUB_TITLE, menu.hub_keyboard()
    if name == "quiet":
        active = is_on_vacation()
        if active:
            until = vacation_until().astimezone(zoneinfo.ZoneInfo("Europe/Moscow"))
            title = f"💤 <b>Тишина до {until:%d.%m %H:%M} МСК</b>"
        else:
            title = "💤 <b>Тишина выключена</b>"
        return title + "\nВключить на:", menu.quiet_keyboard(active)
    if name == "reports":
        return "📈 <b>Отчёты</b>", menu.reports_keyboard()
    if name == "days":
        return "📅 <b>Отчёт за день</b>", menu.days_keyboard(_today_msk(), config.work_days)
    if name == "weeks":
        return "🗓 <b>Недельная сводка</b>", menu.weeks_keyboard(_today_msk())
    if name == "ai":
        on = await db.get_setting("ai_summary_enabled", "1") == "1"
        return "🤖 <b>AI</b>", menu.ai_keyboard(on)
    if name == "maint":
        return ("🛠 <b>Обслуживание AI</b>\nТяжёлые операции, каждая спросит подтверждение.",
                menu.maint_keyboard())
    if name == "hours":
        return ("⏳ <b>Часы на кнопках «Беру»</b>\nОтметь от 1 до 6 вариантов. "
                "Новые тикеты получат их сразу, уже отправленные — нет.",
                menu.hours_keyboard(await busy_options()))
    if name == "howto":
        return menu.HOWTO_TEXT, menu.back_keyboard()
    return None


async def _redraw(callback: CallbackQuery, screen) -> None:
    text, kb = screen
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:  # "message is not modified" on a repeated tap
        logger.debug("cb_menu: screen not redrawn: %s", exc)


async def _run_maint(callback: CallbackQuery, cmd: str) -> None:
    from aiogram.filters import CommandObject
    from ..config import config
    from . import ai_commands, optimizer_commands

    target = callback.message
    if cmd in ("aioptimize", "promptrollback") and callback.from_user.id not in config.operator_telegram_user_ids:
        await target.answer("⛔ Только для администратора бота")
        return
    if cmd == "aiimport":
        await ai_commands.cmd_aiimport(target, CommandObject(command="aiimport"))
    elif cmd == "aireindex":
        await ai_commands.cmd_aireindex(target)
    elif cmd == "aibackfill":
        await ai_commands.cmd_aibackfill(target)
    elif cmd == "aianalyze":
        await ai_commands.cmd_aianalyze(target)
    elif cmd == "aioptimize":
        await optimizer_commands.run_aioptimize(target)
    elif cmd == "promptrollback":
        await optimizer_commands.show_promptrollback(target)


async def _toggle_hour(callback: CallbackQuery, option: str) -> None:
    from ..general_channel import TAKE_HOURS_SETTING, busy_options, is_busy_option

    if not is_busy_option(option):
        await callback.answer()
        return
    new = menu.toggle_hour(await busy_options(), option)
    if new is None:
        await callback.answer("От 1 до 6 вариантов", show_alert=True)
        return
    await db.set_setting(TAKE_HOURS_SETTING, " ".join(new))
    await callback.answer()
    await _redraw(callback, await menu_screen("hours"))


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery) -> None:
    _, action, *rest = callback.data.split(":")
    arg = rest[0] if rest else ""

    if action == "hour":
        await _toggle_hour(callback, arg)
        return
    await callback.answer()

    screen = await menu_screen(action)
    if screen is not None:
        await _redraw(callback, screen)
        return

    target = callback.message
    if action == "status":
        await target.answer(await status_text(), parse_mode="HTML")
    elif action == "refresh":
        await run_refresh(target)
    elif action == "vac":
        until = vacation_until_from_args("" if arg == "next" else f"{arg}d", datetime.now(timezone.utc))
        if until is not None:
            await turn_on_vacation(target, until)
            await _redraw(callback, await menu_screen("quiet"))
    elif action == "workon":
        await turn_off_vacation(target)
        await _redraw(callback, await menu_screen("quiet"))
    elif action in ("report_yesterday", "rep", "week"):
        from . import report_commands
        try:
            day = date.fromisoformat(arg) if arg else None
        except ValueError:
            logger.warning("cb_menu: bad date %r", arg)
            return
        if action == "week":
            await report_commands.run_weekly(target, day)
        else:
            await report_commands.run_daily_report(target, day)
    elif action == "digest":
        try:
            await send_morning_digest(callback.bot)
            await target.answer("🌅 Утренняя сводка отправлена.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("cb_menu digest failed")
            await target.answer(f"⚠️ <b>Ошибка:</b> {exc}", parse_mode="HTML")
    elif action == "aisummary":
        new = "0" if await db.get_setting("ai_summary_enabled", "1") == "1" else "1"
        await db.set_setting("ai_summary_enabled", new)
        await _redraw(callback, await menu_screen("ai"))
    elif action == "aimetrics":
        from .ai_commands import cmd_aimetrics
        await cmd_aimetrics(target)
    elif action == "aiknowledge":
        from .ai_commands import cmd_aiknowledge
        await cmd_aiknowledge(target)
    elif action == "ask" and arg in menu.MAINT_COMMANDS:
        await target.edit_text(f"Запустить «{menu.MAINT_COMMANDS[arg]}»?",
                               reply_markup=menu.confirm_keyboard(arg))
    elif action == "run" and arg in menu.MAINT_COMMANDS:
        await _redraw(callback, await menu_screen("maint"))
        await _run_maint(callback, arg)
    else:
        logger.warning("cb_menu: unknown action %r", callback.data)


def vacation_until_from_args(args: str, now_utc: datetime) -> datetime | None:
    """'' → next work start, '3d' → now + 3 days, 'YYYY-MM-DD' → 09:00 MSK that day; None if unparseable."""
    from .. import work_schedule
    import zoneinfo

    args = args.strip()
    if not args:
        return work_schedule.next_work_start()
    if args.endswith("d") and args[:-1].isdigit():
        return now_utc + timedelta(days=int(args[:-1]))
    try:
        d = date.fromisoformat(args)
    except ValueError:
        return None
    msk = zoneinfo.ZoneInfo("Europe/Moscow")
    return datetime(d.year, d.month, d.day, 9, 0, tzinfo=msk).astimezone(timezone.utc)


async def turn_on_vacation(target: Message, until: datetime) -> None:
    from ..work_schedule import enable_vacation
    import zoneinfo

    await enable_vacation(until)
    until_msk = until.astimezone(zoneinfo.ZoneInfo("Europe/Moscow"))
    await target.answer(
        f"🏖 <b>Режим отпуска включён</b>\n"
        f"Уведомления возобновятся: <b>{until_msk.strftime('%d.%m.%Y %H:%M')} МСК</b>",
        parse_mode="HTML",
    )


async def turn_off_vacation(target: Message) -> None:
    from ..work_schedule import disable_vacation, is_on_vacation

    if not is_on_vacation():
        await target.answer("ℹ️ Режим отпуска не активен.")
        return
    await disable_vacation()
    await target.answer("✅ <b>Режим отпуска отключён.</b> Уведомления возобновлены.", parse_mode="HTML")


@router.message(Command("vacation"))
async def cmd_vacation(message: Message, command: CommandObject) -> None:
    """
    /vacation        — до следующего рабочего дня
    /vacation 3d     — на N дней
    /vacation 2026-04-15  — до конкретной даты (МСК 09:00)
    """
    until = vacation_until_from_args(command.args or "", datetime.now(timezone.utc))
    if until is None:
        await message.answer(
            "⚠️ Неверный формат. Примеры:\n"
            "/vacation — до следующего рабочего дня\n"
            "/vacation 3d — на 3 дня\n"
            "/vacation 2026-04-15 — до 15 апреля",
            parse_mode="HTML",
        )
        return
    await turn_on_vacation(message, until)


@router.message(Command("workon"))
async def cmd_workon(message: Message) -> None:
    """Снять режим отпуска досрочно."""
    await turn_off_vacation(message)


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    await send_morning_digest(message.bot)


async def run_refresh(target: Message) -> None:
    wait_msg = await target.answer("🔄 Синхронизирую с HDE...")
    try:
        result = await refresh_topics(bot=target.bot)
        text = format_refresh_result(
            active_count=result.active_after,
            hde_count=result.hde_count,
            created=result.created,
            renamed=result.renamed,
            deleted=result.deleted,
            cleaned_pending=result.cleaned_pending,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("refresh failed")
        text = f"⚠️ <b>Ошибка при синхронизации:</b> {exc}"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("refresh: wait message cleanup failed: %s", exc)
    await target.answer(text, parse_mode="HTML")

    # Retry AI summaries for topics that missed them
    from ..topic_manager import retry_missing_ai_summaries
    try:
        sent = await retry_missing_ai_summaries(target.bot)
        if sent:
            await target.answer(
                f"🧠 <b>AI саммари отправлено:</b> {sent} топик(ов)",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("retry_missing_ai_summaries failed: %s", exc)


@router.message(Command("refresh"))
async def cmd_refresh(message: Message) -> None:
    await run_refresh(message)


@router.edited_message(F.message_thread_id.is_not(None))
async def on_edited_message(message: Message) -> None:
    """When operator edits a message in a topic, sync the edit to HDE."""
    if message.from_user is None or message.from_user.is_bot:
        return

    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
            chat_id=message.chat.id,
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
from .topic_actions import router as _topic_router  # noqa: E402
from .media_commands import router as _media_router  # noqa: E402

router.include_router(_ai_router)
router.include_router(_optimizer_router)
router.include_router(_report_router)
# Before media: it must see an engineer's documents before the call-recording
# handler (F.document) and the catch-all media cache swallow them.
router.include_router(_topic_router)
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
from .report_commands import cmd_report, cmd_weekly  # noqa: E402,F401
from .media_commands import (  # noqa: E402,F401
    cache_topic_media,
    handle_topic_call_recording,
    handle_topic_voice_note,
)
