from datetime import date, datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ..db import (
    count_active_topics,
    count_pending_delete_topics,
    count_pending_pre_sla_topics,
    count_total_topics,
    mark_report_sent,
)
from ..digest import send_morning_digest
from ..formatter import format_refresh_result
from ..refresh import refresh_topics
from ..operator_replies import (
    OperatorReplyError,
    add_internal_note,
    cache_incoming_topic_media,
    delete_operator_message,
    edit_operator_message,
    format_operator_exception,
    get_operator_topic_context,
    send_public_reply,
)

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


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>HDE Notification Router</b>\n\n"
        "Бот отслеживает ваши тикеты из HelpDeskEddy, ведет Telegram topics,\n"
        "считает pre-SLA напоминания и поддерживает /note и /send для текста, медиа и альбомов.",
        parse_mode="HTML",
    )


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
        f"📚 Всего записей: <b>{total_topics}</b>",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📖 <b>Команды бота</b>\n\n"
        "/start — краткое описание бота\n"
        "/status — активные topics, pending delete и pre-SLA\n"
        "/help — этот список команд\n"
        "/note текст — добавить внутренний комментарий в HDE\n"
        "/note в ответ на фото, видео, voice, документ или альбом — сохранить медиа в комментарий\n"
        "/send текст — отправить публичный ответ клиенту через HDE\n"
        "/send в ответ на сообщение, медиа или альбом — отправить текст, caption и вложения клиенту\n"
        "/delete в ответ на сообщение — удалить его из HDE\n"
        "/refresh — синхронизировать топики с HDE, убрать устаревшие\n"
        "/digest — вручную вызвать утреннюю сводку\n"
        "/vacation — включить режим тишины до следующего рабочего дня\n"
        "/vacation 3d — режим тишины на N дней\n"
        "/vacation YYYY-MM-DD — режим тишины до конкретной даты\n"
        "/workon — снять режим тишины досрочно\n"
        "/report — записать отчёт по операторам в Google Sheets (за вчера)\n"
        "/report YYYY-MM-DD — отчёт за конкретную дату\n"
        "/aisummary — статус AI саммари\n"
        "/aisummary on/off — включить/выключить AI саммари\n"
        "/aiknowledge — статистика базы знаний AI\n",
        parse_mode="HTML",
    )


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


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject) -> None:
    """
    /report          — отчёт за вчера
    /report 2025-04-04  — отчёт за конкретную дату (YYYY-MM-DD)
    """
    from ..reporting.runner import is_report_configured, run_report

    if not is_report_configured():
        await message.answer(
            "⚠️ Отчёт не настроен.\n\n"
            "Добавьте в .env:\n"
            "<code>HDE_REPORT_PASSWORD=...\n"
            "GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google_service_account.json\n"
            "GOOGLE_SPREADSHEET_ID=...</code>",
            parse_mode="HTML",
        )
        return

    report_date: date | None = None
    if command.args:
        try:
            report_date = date.fromisoformat(command.args.strip())
        except ValueError:
            await message.answer(
                "⚠️ Неверный формат даты. Используйте: <code>/report YYYY-MM-DD</code>",
                parse_mode="HTML",
            )
            return

    # Check if target date was a work day
    from ..config import config
    target = report_date if report_date else date.today() - timedelta(days=1)
    target_weekday = target.weekday()
    if target_weekday not in config.work_days:
        await message.answer(
            f"🏖 <b>{target.strftime('%d.%m.%Y')} был выходной</b> — данных для отчёта нет.",
            parse_mode="HTML",
        )
        return

    wait_msg = await message.answer("⏳ Отчёт генерируется, подождите...")
    try:
        result = await run_report(report_date)
        from ..scheduler import mark_report_done_today
        mark_report_done_today()
        from datetime import timedelta
        actual_date = report_date if report_date else date.today() - timedelta(days=1)
        await mark_report_sent(actual_date)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception:
        pass
    await message.answer(result, parse_mode="HTML")


@router.callback_query(F.data == "report:cancel")
async def cb_report_cancel(callback: CallbackQuery) -> None:
    """Inline button: dismiss the report reminder without running the report."""
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data == "report:run_yesterday")
async def cb_report_yesterday(callback: CallbackQuery) -> None:
    """Inline button: run yesterday's report from the personal chat prompt."""
    from ..reporting.runner import run_report
    from ..scheduler import mark_report_done_today

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    from ..work_schedule import was_yesterday_work_day
    if not was_yesterday_work_day():
        yesterday = date.today() - timedelta(days=1)
        await callback.message.answer(
            f"🏖 <b>Вчера ({yesterday.strftime('%d.%m.%Y')}) был выходной</b> — данных для отчёта нет.",
            parse_mode="HTML",
        )
        return

    wait_msg = await callback.message.answer("⏳ Отчёт генерируется, подождите...")
    try:
        result = await run_report()
        mark_report_done_today()
        yesterday = date.today() - timedelta(days=1)
        await mark_report_sent(yesterday)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception:
        pass
    await callback.message.answer(result, parse_mode="HTML")


@router.message(Command("vacation"))
async def cmd_vacation(message: Message, command: CommandObject) -> None:
    """
    /vacation        — до следующего рабочего дня
    /vacation 3d     — на N дней
    /vacation 2026-04-15  — до конкретной даты (МСК полночь)
    """
    from ..work_schedule import next_work_start, set_vacation, vacation_until
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

    set_vacation(until)
    until_msk = until.astimezone(_MSK)
    await message.answer(
        f"🏖 <b>Режим отпуска включён</b>\n"
        f"Уведомления возобновятся: <b>{until_msk.strftime('%d.%m.%Y %H:%M')} МСК</b>",
        parse_mode="HTML",
    )


@router.message(Command("workon"))
async def cmd_workon(message: Message) -> None:
    """Снять режим отпуска досрочно."""
    from ..work_schedule import is_on_vacation, set_vacation

    if not is_on_vacation():
        await message.answer("ℹ️ Режим отпуска не активен.")
        return

    set_vacation(None)
    await message.answer("✅ <b>Режим отпуска отключён.</b> Уведомления возобновлены.", parse_mode="HTML")


@router.message(Command("aisummary"))
async def cmd_aisummary(message: Message, command: CommandObject) -> None:
    """
    /aisummary       — текущий статус
    /aisummary on    — включить AI саммари
    /aisummary off   — выключить AI саммари
    """
    from ..db import get_setting, set_setting

    args = (command.args or "").strip().lower()
    current = await get_setting("ai_summary_enabled", "1")

    if not args:
        status = "включено ✅" if current == "1" else "выключено ❌"
        await message.answer(
            f"🧠 <b>AI Саммари</b>: {status}\n\n"
            "Команды:\n"
            "/aisummary on — включить\n"
            "/aisummary off — выключить",
            parse_mode="HTML",
        )
        return

    if args == "on":
        await set_setting("ai_summary_enabled", "1")
        await message.answer("🧠 <b>AI Саммари включён</b> ✅", parse_mode="HTML")
    elif args == "off":
        await set_setting("ai_summary_enabled", "0")
        await message.answer("🧠 <b>AI Саммари выключен</b> ❌", parse_mode="HTML")
    else:
        await message.answer(
            "⚠️ Используйте: /aisummary on или /aisummary off",
            parse_mode="HTML",
        )


@router.message(Command("aiknowledge"))
async def cmd_aiknowledge(message: Message) -> None:
    """Show knowledge base statistics."""
    from ..db import count_knowledge_by_source
    counts = await count_knowledge_by_source()
    if not counts:
        await message.answer(
            "📚 <b>База знаний пуста</b>\n\nОценивай саммари кнопками 👍/✏️ чтобы накапливать примеры.",
            parse_mode="HTML",
        )
        return
    source_labels = {
        "feedback": "👍 Оценённые ответы",
        "corrected": "✏️ Исправленные ответы",
        "teamly": "🏢 Teamly KB",
        "doc": "📄 Внешние статьи",
        "transcription": "🎙️ Транскрипции звонков",
        "macro": "🔧 Макросы HDE",
    }
    total = sum(counts.values())
    lines = [f"📚 <b>База знаний: {total} записей</b>", ""]
    for source, count in sorted(counts.items(), key=lambda x: -x[1]):
        label = source_labels.get(source, source)
        lines.append(f"• {label}: <b>{count}</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


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
    except Exception:
        pass
    await message.answer(error_text or result_text, parse_mode="HTML")


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
    except Exception:
        return

    await message.answer(result, parse_mode="HTML")


@router.message()
async def cache_topic_media(message: Message) -> None:
    await cache_incoming_topic_media(message)
