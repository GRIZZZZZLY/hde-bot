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


@router.message(Command("aistatus"))
async def cmd_aistatus(message: Message) -> None:
    """AI Knowledge System health overview."""
    from ..db import (
        count_knowledge_by_source,
        count_items_without_embedding,
        get_last_knowledge_item_date,
        get_setting,
    )
    from ..config import config
    from datetime import datetime, timezone

    counts = await count_knowledge_by_source()
    without_emb = await count_items_without_embedding()
    last_item_at = await get_last_knowledge_item_date()
    last_import_at = await get_setting("last_bulk_import_at", "")

    total = sum(counts.values())
    rag_status = "активен ✅" if config.gemini_api_key else "недоступен ❌ (нет Gemini key)"

    source_labels = {
        "feedback": "👍 feedback",
        "corrected": "✏️ corrected",
        "hde_closed": "📥 HDE import",
        "teamly": "🏢 Teamly",
        "doc": "📄 Внешние статьи",
        "macro": "🔧 Макросы HDE",
        "transcription": "🎙️ Транскрипции",
    }

    lines: list[str] = ["🧠 <b>AI Knowledge Status</b>", ""]
    lines.append(f"📚 База знаний: <b>{total} записей</b>")
    if without_emb:
        lines.append(f"   ⚠️ Без embedding: {without_emb} — /aireindex чтобы исправить")

    if counts:
        lines.append("")
        lines.append("По источникам:")
        for source, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            label = source_labels.get(source, source)
            lines.append(f"• {label}: <b>{cnt}</b>")

    lines.append("")
    lines.append(f"🔍 RAG: {rag_status}")

    if last_item_at:
        try:
            dt = datetime.fromisoformat(last_item_at)
            delta = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
            hours = int(delta.total_seconds() // 3600)
            if hours < 1:
                age = "менее часа назад"
            elif hours < 24:
                age = f"{hours} ч. назад"
            else:
                age = f"{delta.days} дн. назад"
            lines.append(f"🕐 Последнее пополнение: {age}")
        except Exception:
            pass

    if last_import_at:
        try:
            dt = datetime.fromisoformat(last_import_at)
            delta = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
            lines.append(f"📥 Последний bulk-импорт: {delta.days} дн. назад")
        except Exception:
            pass

    warnings: list[str] = []
    if not config.gemini_api_key:
        warnings.append("Gemini API key не настроен — RAG и embeddings недоступны")
    if without_emb:
        warnings.append(f"{without_emb} записей без embedding — /aireindex")
    if last_item_at:
        try:
            dt = datetime.fromisoformat(last_item_at)
            delta = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
            if delta.days >= 7:
                warnings.append("База не пополнялась 7+ дней")
        except Exception:
            pass

    if warnings:
        lines.append("")
        lines.append("⚠️ <b>Предупреждения:</b>")
        for w in warnings:
            lines.append(f"• {w}")

    await message.answer("\n".join(lines), parse_mode="HTML")


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
        try:
            tickets = await client.get_closed_tickets(owner_id, limit=limit)
        except HDEApiError as exc:
            errors += 1
            try:
                await wait_msg.edit_text(
                    f"❌ Ошибка HDE API для оператора {owner_id}: {exc}", parse_mode="HTML"
                )
            except Exception:
                pass
            continue

        for i, ticket_raw in enumerate(tickets, 1):
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

            # Fetch full conversation
            try:
                info = await client.get_ticket_info(ticket_id)
                posts = await client.get_ticket_posts(ticket_id)
            except Exception as exc:
                logger.warning("Failed to fetch ticket %s: %s", ticket_id, exc)
                errors += 1
                await asyncio.sleep(0.5)
                continue

            history = _build_history_text(posts, info)
            if not history.strip():
                skipped += 1
                continue

            # Fetch organization (cached per user to avoid duplicate API calls)
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
                # Save content_hash for deduplication
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

            now = asyncio.get_event_loop().time()
            if now - last_edit_at >= 2.0:
                try:
                    await wait_msg.edit_text(
                        f"📥 <b>[{i}/{len(tickets)}]</b> {ticket_title[:50]}\n"
                        f"✅ {added} добавлено · ⏭ {skipped} дублей · ❌ {errors} ошибок",
                        parse_mode="HTML",
                    )
                    last_edit_at = now
                except Exception:
                    pass

            await asyncio.sleep(0.5)

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
