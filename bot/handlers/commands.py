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
    mark_report_sent,
)
from .. import db
from ..ai_summary import invalidate_prompt_cache
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
        "/aiknowledge — статистика базы знаний AI\n"
        "/aistatus — статус AI Knowledge System (RAG, embedding, предупреждения)\n"
        "/aiimport [N] [owner_id] — bulk-импорт закрытых тикетов HDE\n"
        "/aireindex — переиндексировать записи без embedding\n"
        "/aibackfill — дозаполнить организации в базе знаний\n",
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
    from ..work_schedule import last_work_day
    target = report_date if report_date else last_work_day()
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
        await mark_report_sent(target)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception:
        pass
    await message.answer(result, parse_mode="HTML")


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
    except Exception:
        pass

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
        except Exception:
            pass
        return

    owner_name = config.hde_owner_name or "Оператор"
    try:
        await callback.message.edit_text(
            f"✅ <b>Забрал {owner_name}</b>\n\n{callback.message.text or ''}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    # Remove DB record so on_owner_changed webhook skips deletion
    await db.delete_general_message(ticket_id)
    await callback.answer(f"Тикет {ticket_id} назначен на тебя")


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


@router.message(Command("aimetrics"))
async def cmd_aimetrics(message: Message) -> None:
    """Показать метрики базы знаний и кнопки управления."""
    from ..db import get_knowledge_metrics
    from ..scheduler import KNOWLEDGE_EXPIRY_DAYS
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    metrics = await get_knowledge_metrics()
    by_source = metrics["by_source"]

    # Строки по источникам
    source_lines = []
    labels = {
        "hde_closed": "из закрытых тикетов HDE (/aiimport)",
        "implicit_good": "подтверждены операторами",
        "implicit_corrected": "исправления AI-ответов",
        "feedback": "ручной фидбек",
    }
    for src, label in labels.items():
        count = by_source.get(src, 0)
        if count:
            source_lines.append(f"  • {count:4d} — {label}")
    other_sources = {k: v for k, v in by_source.items() if k not in labels}
    for src, count in other_sources.items():
        source_lines.append(f"  • {count:4d} — {src}")

    sources_text = "\n".join(source_lines) if source_lines else "  (пусто)"

    # Паттерны
    patterns_text = ""
    if metrics["top_patterns"]:
        lines = []
        for i, p in enumerate(metrics["top_patterns"], 1):
            eq = p["equipment"] or "Без бренда"
            lines.append(f"{i}. {eq} — {p['problem_type']} ({p['use_count']} раз)")
        patterns_text = "\n🔥 <b>Топ-5 паттернов решений:</b>\n" + "\n".join(lines)

    # Мёртвые элементы
    dead_text = ""
    if metrics["dead_items"]:
        lines = []
        for p in metrics["dead_items"]:
            date = (p["created_at"] or "")[:10]
            title = (p["title"] or "без заголовка")[:60]
            lines.append(f"[{date}] {title}")
        dead_text = "\n💀 <b>Топ-5 без использования:</b>\n" + "\n".join(lines)

    # Предупреждения
    warnings = []
    if metrics["expired_count"]:
        warnings.append(
            f"⚠️ Устарело (&gt;{KNOWLEDGE_EXPIRY_DAYS} дн, не используются): "
            f"<b>{metrics['expired_count']}</b>"
        )
    if metrics["no_embedding_count"]:
        warnings.append(
            f"⚠️ Без эмбеддинга: <b>{metrics['no_embedding_count']}</b> → /aireindex"
        )
    warnings_text = ("\n" + "\n".join(warnings)) if warnings else ""

    text = (
        f"📊 <b>База знаний:</b> {metrics['total']} записей\n"
        f"{sources_text}"
        f"{warnings_text}"
        f"{patterns_text}"
        f"{dead_text}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Дедуп", callback_data="km_dedup")
    builder.button(text="🗑 Удалить устаревшие", callback_data="km_expire")
    builder.adjust(2)

    await message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())


@router.callback_query(F.data == "km_dedup")
async def cb_km_dedup(callback: CallbackQuery) -> None:
    from ..db import dedup_knowledge_items
    count = await dedup_knowledge_items()
    await callback.answer(f"🧹 Помечено дублей: {count}", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data == "km_expire")
async def cb_km_expire(callback: CallbackQuery) -> None:
    from ..db import expire_stale_knowledge
    from ..scheduler import KNOWLEDGE_EXPIRY_DAYS
    count = await expire_stale_knowledge(KNOWLEDGE_EXPIRY_DAYS)
    await callback.answer(f"🗑 Помечено устаревших: {count}", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.message(Command("aistatus"))
async def cmd_aistatus(message: Message) -> None:
    """Алиас для /aimetrics (обратная совместимость)."""
    await cmd_aimetrics(message)


@router.message(Command("aiimport"))
async def cmd_aiimport(message: Message, command: CommandObject) -> None:
    """Bulk-import closed tickets from HDE into the knowledge base.

    Usage:
        /aiimport          — 50 own tickets
        /aiimport 100      — 100 own tickets
        /aiimport 50 456   — tickets of operator 456
        /aiimport 50 456,789 — tickets of two operators
    """
    import asyncio
    import hashlib
    import aiosqlite
    from ..config import config
    from ..db import DB_PATH, save_knowledge_item, set_setting
    from ..hde_api import HDEApiClient, HDEApiError
    from ..ai_summary import _build_history_text
    from ..knowledge.indexer import index_knowledge_item
    from datetime import datetime, timezone

    # Parse args
    raw = (command.args or "").strip().split()
    limit = 50
    owner_ids: list[str] = [config.hde_owner_id] if config.hde_owner_id else []

    if raw:
        if raw[0].isdigit():
            limit = int(raw[0])
            if len(raw) > 1:
                owner_ids = [x.strip() for x in raw[1].split(",") if x.strip()]
        else:
            owner_ids = [x.strip() for x in raw[0].split(",") if x.strip()]

    if not owner_ids:
        await message.answer(
            "⚠️ HDE_OWNER_ID не настроен и owner_id не указан.\n"
            "Использование: /aiimport [N] [owner_id,owner_id2]",
            parse_mode="HTML",
        )
        return

    if not config.has_hde_api_credentials():
        await message.answer("⚠️ HDE API не настроен (HDE_API_EMAIL / HDE_API_KEY).", parse_mode="HTML")
        return

    wait_msg = await message.answer(
        f"📥 Импортирую закрытые тикеты (до {limit} на оператора, операторов: {len(owner_ids)})...",
        parse_mode="HTML",
    )

    client = HDEApiClient()
    added = 0
    skipped = 0
    errors = 0
    last_edit_at = 0.0  # timestamp of last wait_msg edit
    org_cache: dict[str, tuple[str, str]] = {}  # user_id → (org_id, org_name)

    for owner_id in owner_ids:
        page = 1
        total_pages = 1

        while added < limit and page <= total_pages:
            try:
                page_tickets, total_pages = await client.get_closed_tickets_page(
                    owner_id, page=page
                )
            except HDEApiError as exc:
                errors += 1
                try:
                    await wait_msg.edit_text(
                        f"❌ Ошибка HDE API для оператора {owner_id} (стр. {page}): {exc}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                break

            if not page_tickets:
                break

            for ticket_raw in page_tickets:
                if added >= limit:
                    break

                ticket_id = str(ticket_raw.get("id") or "")
                ticket_title = str(ticket_raw.get("title") or "")

                if not ticket_id:
                    errors += 1
                    continue

                # Deduplication
                content_hash = hashlib.sha256(f"hde:{ticket_id}".encode()).hexdigest()
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute(
                        "SELECT id FROM knowledge_items WHERE content_hash = ?", (content_hash,)
                    ) as cur:
                        exists = await cur.fetchone()
                if exists:
                    skipped += 1
                    continue

                # Fetch full conversation (posts + internal comments)
                try:
                    info = await client.get_ticket_info(ticket_id)
                    posts = await client.get_ticket_posts(ticket_id)
                    try:
                        comments = await client.get_ticket_comments(ticket_id)
                    except Exception:
                        comments = []
                    all_posts = sorted(posts + comments, key=lambda p: p.date_created)
                except Exception as exc:
                    logger.warning("Failed to fetch ticket %s: %s", ticket_id, exc)
                    errors += 1
                    await asyncio.sleep(0.5)
                    continue

                history = _build_history_text(all_posts, info)
                if not history.strip():
                    skipped += 1
                    continue

                # Fetch organization (cached per user)
                user_id_str = str(info.client_id)
                if user_id_str not in org_cache:
                    org_cache[user_id_str] = await client.get_user_organization(user_id_str)
                org_id, org_name = org_cache[user_id_str]
                company_id = org_id or user_id_str
                company_name = org_name or info.client_name or ""
                content = f"Тема: {ticket_title}\n\n{history}"

                # Index (embed + save)
                item_id = await index_knowledge_item(
                    source="hde_closed",
                    content=content,
                    ticket_id=ticket_id,
                    title=ticket_title,
                    quality="good",
                    company_id=company_id,
                    company_name=company_name,
                )
                if item_id is not None:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE knowledge_items SET content_hash = ? WHERE id = ?",
                            (content_hash, item_id),
                        )
                        await db.commit()
                else:
                    # No embedding yet — save text only, index later with /aireindex
                    item_id = await save_knowledge_item(
                        source="hde_closed",
                        content=content,
                        ticket_id=ticket_id,
                        title=ticket_title,
                        quality="good",
                        content_hash=content_hash,
                        company_id=company_id,
                        company_name=company_name,
                    )
                added += 1
                # Update wiki article (non-fatal)
                try:
                    from ..wiki.builder import build_or_update_wiki_article
                    await build_or_update_wiki_article(
                        title=ticket_title,
                        content=content,
                        ticket_id=ticket_id,
                    )
                except Exception as exc:
                    logger.warning("Wiki update failed for ticket %s: %s", ticket_id, exc)

                now = asyncio.get_event_loop().time()
                if now - last_edit_at >= 2.0:
                    try:
                        await wait_msg.edit_text(
                            f"📥 Стр. {page}/{total_pages} · {ticket_title[:40]}\n"
                            f"✅ {added}/{limit} · ⏭ {skipped} дублей · ❌ {errors} ошибок",
                            parse_mode="HTML",
                        )
                        last_edit_at = now
                    except Exception:
                        pass

                await asyncio.sleep(0.5)

            page += 1

    await set_setting("last_bulk_import_at", datetime.now(timezone.utc).isoformat())

    try:
        await wait_msg.delete()
    except Exception:
        pass

    await message.answer(
        f"✅ <b>Импорт завершён</b>\n\n"
        f"• Добавлено: <b>{added}</b>\n"
        f"• Пропущено (дубли): <b>{skipped}</b>\n"
        f"• Ошибок: <b>{errors}</b>",
        parse_mode="HTML",
    )


@router.message(Command("aibackfill"))
async def cmd_aibackfill(message: Message) -> None:
    """Backfill company_id/company_name for HDE tickets imported without org info."""
    import asyncio
    from ..db import list_items_without_company, update_knowledge_company
    from ..hde_api import HDEApiClient, HDEApiError

    items = await list_items_without_company()
    if not items:
        await message.answer("✅ У всех записей уже заполнена организация.", parse_mode="HTML")
        return

    wait_msg = await message.answer(
        f"🔄 Дозаполняю организацию для {len(items)} записей...", parse_mode="HTML"
    )

    client = HDEApiClient()
    org_cache: dict[str, tuple[str, str]] = {}
    done = 0
    errors = 0
    last_edit_at = 0.0

    for item_id, ticket_id in items:
        try:
            info = await client.get_ticket_info(ticket_id)
            user_id_str = str(info.client_id)
            if user_id_str not in org_cache:
                org_cache[user_id_str] = await client.get_user_organization(user_id_str)
            org_id, org_name = org_cache[user_id_str]
            company_id = org_id or user_id_str
            company_name = org_name or info.client_name or ""
            await update_knowledge_company(item_id, company_id, company_name)
            done += 1
        except Exception as exc:
            logger.warning("aibackfill: failed for item_id=%s ticket=%s: %s", item_id, ticket_id, exc)
            errors += 1

        now = asyncio.get_event_loop().time()
        if now - last_edit_at >= 2.0:
            try:
                await wait_msg.edit_text(
                    f"🔄 <b>[{done + errors}/{len(items)}]</b> заполнено: {done} · ошибок: {errors}",
                    parse_mode="HTML",
                )
                last_edit_at = now
            except Exception:
                pass

        await asyncio.sleep(0.3)

    try:
        await wait_msg.delete()
    except Exception:
        pass

    await message.answer(
        f"✅ <b>Дозаполнено: {done}</b> записей\n"
        f"{'⚠️ Ошибок: ' + str(errors) if errors else ''}",
        parse_mode="HTML",
    )


@router.message(Command("aireindex"))
async def cmd_aireindex(message: Message) -> None:
    """Regenerate embeddings for knowledge items that are missing them.

    Use after /aiimport when Gemini key was not configured,
    or after switching LLM providers.
    """
    from ..db import list_knowledge_items_without_embedding, update_knowledge_embedding
    from ..knowledge.indexer import embed_text
    from ..knowledge.store import embedding_to_bytes

    items = await list_knowledge_items_without_embedding()
    if not items:
        await message.answer("✅ Все записи уже проиндексированы.", parse_mode="HTML")
        return

    wait_msg = await message.answer(
        f"🔄 Переиндексирую {len(items)} записей...", parse_mode="HTML"
    )
    done = 0
    errors = 0
    for item_id, content in items:
        emb = await embed_text(content)
        if emb is None:
            errors += 1
            continue
        await update_knowledge_embedding(item_id, embedding_to_bytes(emb))
        done += 1

    try:
        await wait_msg.delete()
    except Exception:
        pass

    result = f"✅ <b>Переиндексировано: {done}</b> записей"
    if errors:
        result += f"\n⚠️ Ошибок: {errors} (нет Gemini key или API недоступен)"
    await message.answer(result, parse_mode="HTML")


@router.message(Command("aianalyze"))
async def cmd_aianalyze(message: Message) -> None:
    """Analyze knowledge_items and populate solution_patterns table."""
    import aiohttp
    import aiosqlite
    from ..db import (
        save_solution_pattern,
        pattern_exists_similar,
        count_solution_patterns_by_equipment,
        mark_knowledge_items_analyzed,
        normalize_equipment,
        DB_PATH,
    )
    from ..config import config as _config
    import json as _json
    import time as _time
    import re as _re

    if not _config.gemini_api_key and not _config.groq_api_key:
        await message.answer("❌ Ни GEMINI_API_KEY, ни GROQ_API_KEY не настроены")
        return

    # Load only items not yet analyzed
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT id, title, content FROM knowledge_items "
            "WHERE analyzed_at IS NULL "
            "AND source IN ('hde_closed', 'feedback', 'implicit_good') "
            "AND quality NOT IN ('bad', 'expired') AND content != '' "
            "ORDER BY created_at DESC LIMIT 500"
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await message.answer("ℹ️ Нет новых тикетов для анализа — все уже обработаны.\nЗапусти /aiimport чтобы добавить новые.")
        return

    status_msg = await message.answer(f"⏳ Начинаю анализ {len(items)} тикетов...")
    t_start = _time.monotonic()
    batch_size = 15
    created = 0
    skipped = 0

    _ANALYZE_PROMPT = (
        "Ты анализируешь решённые тикеты технической поддержки кассового оборудования.\n"
        "Из каждого тикета извлеки:\n"
        "- equipment: бренд оборудования — используй СТРОГО одно из: "
        "АТОЛ, Эвотор, Штрих-М, Viki, ВикиПринт, Эквайринг Сбер, ВТБ, Т-Банк, ПТК, AQSI, PAX, Posiflora "
        "или null если бренд не определён\n"
        "- problem_type: краткое описание типа проблемы (5-10 слов)\n"
        "- steps: конкретные шаги решения через → (если шагов нет — пропусти тикет)\n\n"
        "Верни JSON-массив: [{\"equipment\": ..., \"problem_type\": ..., \"steps\": ...}, ...]\n"
        "Включай только тикеты с явными шагами решения. Пропускай общие вопросы без решения.\n"
        "Только JSON, без markdown.\n\nТикеты:\n"
    )

    import asyncio as _asyncio
    _RATE_DELAY = 8.0    # seconds between requests — Gemini 2.0 Flash free-tier ~8 RPM
    _RETRY_DELAY = 120.0  # seconds to wait after a 429 before retrying the same batch
    _GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
    _GROQ_MODEL = "llama-3.3-70b-versatile"

    async def _try_groq_batch(session, prompt_text: str) -> list | None:
        """Try to analyze a batch with Groq. Returns parsed list or None on failure."""
        if not _config.groq_api_key:
            return None
        try:
            async with session.post(
                _GROQ_URL,
                json={
                    "model": _GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt_text}],
                    "temperature": 0.1,
                    "max_tokens": 2000,
                },
                headers={"Authorization": f"Bearer {_config.groq_api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("aianalyze Groq HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
                raw = data["choices"][0]["message"]["content"].strip()
                raw = _re.sub(r"^```[^\n]*\n?", "", raw).rstrip("`").strip()
                result = _json.loads(raw)
                if isinstance(result, list):
                    return result
                if isinstance(result, dict):
                    for v in result.values():
                        if isinstance(v, list):
                            return v
                return None
        except Exception as exc:
            logger.warning("aianalyze Groq batch failed: %s", exc)
            return None

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            batch_ids = [row[0] for row in batch]
            batch_text = ""
            for idx, (_, title, content) in enumerate(batch, 1):
                batch_text += f"\n[{idx}] {title}\n{content[:400]}\n"

            full_prompt = _ANALYZE_PROMPT + batch_text

            # --- Groq first ---
            patterns = await _try_groq_batch(session, full_prompt)

            # --- Gemini fallback ---
            if patterns is None:
                payload = {
                    "contents": [{"parts": [{"text": full_prompt}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2000},
                }
                for attempt in range(2):
                    try:
                        async with session.post(
                            (
                                "https://generativelanguage.googleapis.com/v1beta/models/"
                                "gemini-2.0-flash:generateContent"
                            ),
                            json=payload,
                            params={"key": _config.gemini_api_key},
                            timeout=aiohttp.ClientTimeout(total=60),
                        ) as resp:
                            if resp.status == 429:
                                if attempt == 0:
                                    logger.warning(
                                        "aianalyze batch %d: 429 rate limit, waiting %ss",
                                        i, int(_RETRY_DELAY),
                                    )
                                    try:
                                        await status_msg.edit_text(
                                            f"⏳ Обработано {i}/{len(items)} — ожидаю сброса лимита Gemini (~1 мин)..."
                                        )
                                    except Exception:
                                        pass
                                    await _asyncio.sleep(_RETRY_DELAY)
                                    continue
                                logger.warning("aianalyze batch %d: 429 on retry, skipping", i)
                                skipped += len(batch)
                                break
                            if resp.status != 200:
                                body = await resp.text()
                                logger.warning(
                                    "aianalyze batch %d: Gemini HTTP %s: %s",
                                    i, resp.status, body[:300],
                                )
                                skipped += len(batch)
                                break
                            data = await resp.json()
                            raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                            raw = _re.sub(r"^```[^\n]*\n?", "", raw).rstrip("`").strip()
                            patterns = _json.loads(raw)
                            break
                    except Exception as exc:
                        logger.warning("aianalyze batch %d Gemini failed: %s", i, exc)
                        skipped += len(batch)
                        break

            if patterns is None:
                # Both Groq and Gemini failed for this batch — don't mark as analyzed
                skipped += len(batch)
            else:
                for p in patterns:
                    eq = normalize_equipment(p.get("equipment"))
                    pt = (p.get("problem_type") or "").strip()
                    st = (p.get("steps") or "").strip()
                    if not pt or not st:
                        continue
                    if await pattern_exists_similar(eq, pt):
                        skipped += 1
                        continue
                    await save_solution_pattern(
                        problem_type=pt, steps=st, source="analyze", equipment=eq
                    )
                    created += 1
                # Mark these items as analyzed so they're skipped next run
                await mark_knowledge_items_analyzed(batch_ids)

            # Rate-limit pause between every batch (Gemini fallback needs it)
            await _asyncio.sleep(_RATE_DELAY)

            # Update progress every 50 items
            processed = min(i + batch_size, len(items))
            if processed % 50 == 0 or processed == len(items):
                try:
                    await status_msg.edit_text(
                        f"⏳ Обработано {processed}/{len(items)} тикетов... "
                        f"Создано паттернов: {created}"
                    )
                except Exception:
                    pass

    elapsed = int(_time.monotonic() - t_start)
    counts = await count_solution_patterns_by_equipment()
    lines = [f"• {eq} — {cnt} паттернов" for eq, cnt in counts.items()]
    report = (
        f"✅ Анализ завершён за {elapsed} сек.\n"
        f"Обработано тикетов: {len(items)}\n"
        f"Создано паттернов: {created}\n"
        f"Пропущено (дубли/ошибки): {skipped}\n\n"
        "По оборудованию:\n" + "\n".join(lines) + "\n\n"
        "Запусти /aianalyze снова чтобы обновить."
    )
    try:
        await status_msg.edit_text(report)
    except Exception:
        await message.answer(report)


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
    except Exception:
        return

    await message.answer(result, parse_mode="HTML")


@router.message(Command("aioptimize"))
async def cmd_aioptimize(message: Message) -> None:
    """Manually trigger prompt optimizer (for testing)."""
    from ..optimizer.agent import run_optimizer
    from ..config import config as _cfg
    if message.from_user and message.from_user.id not in _cfg.operator_telegram_user_ids:
        return
    await message.answer("🧪 Запускаю оптимизатор промптов...")
    import asyncio
    asyncio.create_task(run_optimizer(message.bot))


@router.callback_query(F.data.startswith("opt:apply:"))
async def cb_opt_apply(callback: CallbackQuery) -> None:
    try:
        version_id = int(callback.data.split(":")[-1])
        await db.apply_prompt_version(version_id)
        invalidate_prompt_cache()
        await callback.answer("✅ Новый промпт применён", show_alert=True)
        try:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n<i>✅ Применено</i>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        await callback.answer(f"❌ Ошибка: {exc}", show_alert=True)


@router.callback_query(F.data == "opt:reject")
async def cb_opt_reject(callback: CallbackQuery) -> None:
    from ..db import reject_all_prompt_candidates
    await reject_all_prompt_candidates()
    await callback.answer("❌ Отклонено", show_alert=False)
    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n<i>❌ Отклонено</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("opt:detail:"))
async def cb_opt_detail(callback: CallbackQuery) -> None:
    from html import escape
    from ..db import get_prompt_version, get_optimization_samples
    from ..ai_summary import get_active_format_instructions
    await callback.answer()
    try:
        version_id = int(callback.data.split(":")[-1])
        version = await get_prompt_version(version_id)
        if not version:
            await callback.message.answer("❌ Версия промпта не найдена.")
            return

        current = await get_active_format_instructions()
        new_text = version["content"]

        # Sample breakdown
        samples = await get_optimization_samples(days=30)
        by_outcome: dict[str, int] = {}
        for s in samples:
            by_outcome[s["outcome"]] = by_outcome.get(s["outcome"], 0) + 1

        outcome_line = "  ".join(
            f"{o}: {c}" for o, c in sorted(by_outcome.items())
        )

        # Send full old prompt
        old_msg = f"📄 <b>Текущий промпт:</b>\n\n<pre>{escape(current[:3800])}</pre>"
        await callback.message.answer(old_msg, parse_mode="HTML")

        # Send full new prompt
        score_str = f"{round(version['score'] * 100)} баллов" if version.get("score") else "—"
        new_msg = (
            f"✨ <b>Предложенный промпт</b> ({escape(version['proposed_by'])}, {score_str}):\n\n"
            f"<pre>{escape(new_text[:3800])}</pre>\n\n"
            f"📊 Сэмплы: {len(samples)} ({outcome_line})"
        )
        await callback.message.answer(new_msg, parse_mode="HTML")

    except Exception as exc:
        await callback.message.answer(f"❌ Ошибка: {exc}")


@router.message()
async def cache_topic_media(message: Message) -> None:
    await cache_incoming_topic_media(message)
