"""AI knowledge-base commands: /aisummary, /aiknowledge, /aimetrics, /aiimport,
/aibackfill, /aireindex, /aianalyze and knowledge-management callbacks."""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)
router = Router()


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
    except Exception as exc:
        logger.debug("km_dedup: reply markup cleanup failed: %s", exc)


@router.callback_query(F.data == "km_expire")
async def cb_km_expire(callback: CallbackQuery) -> None:
    from ..db import expire_stale_knowledge
    from ..scheduler import KNOWLEDGE_EXPIRY_DAYS
    count = await expire_stale_knowledge(KNOWLEDGE_EXPIRY_DAYS)
    await callback.answer(f"🗑 Помечено устаревших: {count}", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("km_expire: reply markup cleanup failed: %s", exc)


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
    from ..config import config
    from ..db import list_knowledge_content_hashes, save_knowledge_item, set_setting
    from ..hde_api import HDEApiClient, HDEApiError, post_sort_key
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
    # One query instead of a per-ticket SELECT; updated in-memory as we insert.
    known_hashes = await list_knowledge_content_hashes()
    # Wiki articles are built AFTER the import loop: each build costs 2 LLM
    # calls, and sequential building avoids same-topic files racing each other.
    wiki_queue: list[tuple[str, str, str]] = []  # (title, content, ticket_id)

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
                except Exception as edit_exc:
                    logger.debug("aiimport: progress edit failed: %s", edit_exc)
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

                # Deduplication (in-memory against the preloaded hash set)
                content_hash = hashlib.sha256(f"hde:{ticket_id}".encode()).hexdigest()
                if content_hash in known_hashes:
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
                    all_posts = sorted(posts + comments, key=post_sort_key)
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
                    content_hash=content_hash,
                    company_id=company_id,
                    company_name=company_name,
                )
                if item_id is None:
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
                known_hashes.add(content_hash)
                wiki_queue.append((ticket_title, content, ticket_id))

                now = asyncio.get_event_loop().time()
                if now - last_edit_at >= 2.0:
                    try:
                        await wait_msg.edit_text(
                            f"📥 Стр. {page}/{total_pages} · {ticket_title[:40]}\n"
                            f"✅ {added}/{limit} · ⏭ {skipped} дублей · ❌ {errors} ошибок",
                            parse_mode="HTML",
                        )
                        last_edit_at = now
                    except Exception as edit_exc:
                        logger.debug("aiimport: progress edit failed: %s", edit_exc)

                await asyncio.sleep(0.5)

            page += 1

    # Wiki phase: knowledge items are already searchable via RAG at this point;
    # article synthesis (2 LLM calls each) runs as a follow-up pass.
    if wiki_queue:
        from ..wiki.builder import build_or_update_wiki_article
        for i, (w_title, w_content, w_ticket_id) in enumerate(wiki_queue, start=1):
            try:
                await build_or_update_wiki_article(
                    title=w_title,
                    content=w_content,
                    ticket_id=w_ticket_id,
                )
            except Exception as exc:
                logger.warning("Wiki update failed for ticket %s: %s", w_ticket_id, exc)
            now = asyncio.get_event_loop().time()
            if now - last_edit_at >= 2.0:
                try:
                    await wait_msg.edit_text(
                        f"📚 Обновляю вики: {i}/{len(wiki_queue)}...",
                        parse_mode="HTML",
                    )
                    last_edit_at = now
                except Exception as edit_exc:
                    logger.debug("aiimport: progress edit failed: %s", edit_exc)

    await set_setting("last_bulk_import_at", datetime.now(timezone.utc).isoformat())

    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("aiimport: wait message cleanup failed: %s", exc)

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
            except Exception as edit_exc:
                logger.debug("aibackfill: progress edit failed: %s", edit_exc)

        await asyncio.sleep(0.3)

    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("aibackfill: wait message cleanup failed: %s", exc)

    await message.answer(
        f"✅ <b>Дозаполнено: {done}</b> записей\n"
        f"{'⚠️ Ошибок: ' + str(errors) if errors else ''}",
        parse_mode="HTML",
    )


@router.message(Command("aireindex"))
async def cmd_aireindex(message: Message) -> None:
    """Regenerate embeddings for knowledge items that are missing them.

    Use after /aiimport when the embedding step failed,
    or after switching LLM providers.
    """
    from ..db import list_knowledge_items_without_embedding, update_knowledge_embeddings
    from ..knowledge import indexer
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
    embeddings = await indexer.embed_texts([content for _, content in items])
    if embeddings is None:
        errors = len(items)
    else:
        pairs = [
            (item_id, embedding_to_bytes(emb))
            for (item_id, _), emb in zip(items, embeddings)
        ]
        await update_knowledge_embeddings(pairs)
        done = len(pairs)

    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("aireindex: wait message cleanup failed: %s", exc)

    result = f"✅ <b>Переиндексировано: {done}</b> записей"
    if errors:
        result += f"\n⚠️ Ошибок: {errors} (embedding-модель недоступна)"
    await message.answer(result, parse_mode="HTML")


@router.message(Command("aianalyze"))
async def cmd_aianalyze(message: Message) -> None:
    """Analyze knowledge_items and populate solution_patterns table."""
    import aiohttp
    from ..db import (
        save_solution_pattern,
        build_pattern_index,
        pattern_similar_in_index,
        list_solution_patterns,
        count_solution_patterns_by_equipment,
        mark_knowledge_items_analyzed,
        normalize_equipment,
        connect,
    )
    from ..config import config as _config
    import json as _json
    import time as _time
    import re as _re

    if not _config.groq_api_key and not _config.openrouter_api_key:
        await message.answer("❌ Ни GROQ_API_KEY, ни OPENROUTER_API_KEY не настроены")
        return

    # Load only items not yet analyzed
    async with connect() as conn:
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
    # Load existing patterns once; dedup runs in memory and the index is
    # extended after each insert so same-run duplicates are caught too.
    pattern_index = build_pattern_index(await list_solution_patterns(limit=100000))

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
    _RATE_DELAY = 8.0    # seconds between requests — keeps well under free-tier RPM limits
    _GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
    # Модели: основная из env, фолбэк — на отдельной per-model квоте Groq.
    # llama-3.3-70b и llama-4-scout Groq вывел из обслуживания (404).
    _GROQ_MODEL = _config.groq_summary_model
    _GROQ_FALLBACK_MODEL = _config.groq_classify_fallback_model
    _OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
    _OPENROUTER_MODEL = _config.openrouter_model

    def _parse_json_list(raw: str) -> list | None:
        raw = _re.sub(r"^```[^\n]*\n?", "", raw).rstrip("`").strip()
        result = _json.loads(raw)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for v in result.values():
                if isinstance(v, list):
                    return v
        return None

    async def _try_openrouter_batch(
        session, prompt_text: str
    ) -> tuple[list | None, str | None]:
        """Returns (patterns, reason). reason is short error string or None on success/skip."""
        if not _config.openrouter_api_key:
            return None, "no_key"
        from ..llm_semaphore import LLM_SEMAPHORE  # noqa: PLC0415
        try:
            async with LLM_SEMAPHORE, session.post(
                _OPENROUTER_URL,
                json={
                    "model": _OPENROUTER_MODEL,
                    "messages": [{"role": "user", "content": prompt_text}],
                    "temperature": 0.1,
                    "max_tokens": 2000,
                },
                headers={"Authorization": f"Bearer {_config.openrouter_api_key}"},
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("aianalyze OpenRouter HTTP %s: %s", resp.status, body[:200])
                    return None, f"HTTP {resp.status}"
                data = await resp.json()
                raw = data["choices"][0]["message"]["content"].strip()
                parsed = _parse_json_list(raw)
                if parsed is None:
                    return None, "parse error"
                return parsed, None
        except _asyncio.TimeoutError:
            return None, "timeout"
        except Exception as exc:
            logger.warning("aianalyze OpenRouter batch failed: %s", exc)
            return None, type(exc).__name__

    async def _try_groq_batch(
        session, prompt_text: str, model: str = _GROQ_MODEL
    ) -> tuple[list | None, str | None]:
        """Returns (patterns, reason). reason is short error string or None on success/skip."""
        if not _config.groq_api_key:
            return None, "no_key"
        from ..llm_semaphore import LLM_SEMAPHORE  # noqa: PLC0415
        try:
            async with LLM_SEMAPHORE, session.post(
                _GROQ_URL,
                json={
                    "model": model,
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
                    return None, f"HTTP {resp.status}"
                data = await resp.json()
                raw = data["choices"][0]["message"]["content"].strip()
                parsed = _parse_json_list(raw)
                if parsed is None:
                    return None, "parse error"
                return parsed, None
        except _asyncio.TimeoutError:
            return None, "timeout"
        except Exception as exc:
            logger.warning("aianalyze Groq batch failed: %s", exc)
            return None, type(exc).__name__

    # Per-model batch counters for the final report.
    model_stats = {"gemma4": 0, "groq": 0, "scout": 0, "failed": 0}
    # Session-wide dedupe: each (from_model, reason) pair is announced at most once per run.
    notified_keys: set[tuple[str, str]] = set()
    # Circuit-breaker: abort after N consecutive "all 3 failed" batches.
    consecutive_all_failed = 0
    _CIRCUIT_BREAKER_THRESHOLD = 3
    # Adaptive pause: bump to 60s after an all-failed batch, reset to 8s on next success.
    _BACKOFF_DELAY = 60.0
    current_delay = _RATE_DELAY

    async def _notify_fallback(
        batch_num: int, from_model: str, reason: str, to_model: str
    ) -> None:
        key = (from_model, reason)
        if key in notified_keys:
            return
        notified_keys.add(key)
        try:
            await message.answer(
                f"⚠️ Батч {batch_num}: {from_model} → {reason}.\n"
                f"Переключаюсь на {to_model}."
            )
        except Exception as exc:
            logger.debug("aianalyze: fallback notice failed: %s", exc)

    async def _notify_all_failed(batch_num: int, reasons: dict[str, str]) -> None:
        key = ("all", "|".join(f"{k}:{v}" for k, v in sorted(reasons.items())))
        if key in notified_keys:
            return
        notified_keys.add(key)
        parts = [f"{m}: {r}" for m, r in reasons.items()]
        try:
            await message.answer(
                f"❌ Батч {batch_num}: все 3 модели упали — пропущено.\n"
                f"Причины: {'; '.join(parts)}"
            )
        except Exception as exc:
            logger.debug("aianalyze: all-failed notice failed: %s", exc)

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            batch_ids = [row[0] for row in batch]
            batch_num = i // batch_size + 1
            batch_text = ""
            for idx, (_, title, content) in enumerate(batch, 1):
                batch_text += f"\n[{idx}] {title}\n{content[:400]}\n"

            full_prompt = _ANALYZE_PROMPT + batch_text
            patterns = None
            reasons: dict[str, str] = {}

            # --- OpenRouter Gemma 4 31B first ---
            patterns, or_reason = await _try_openrouter_batch(session, full_prompt)
            if patterns is not None:
                model_stats["gemma4"] += 1
            else:
                reasons["Gemma 4"] = or_reason or "unknown"
                await _notify_fallback(
                    batch_num, "Gemma 4", or_reason or "unknown", f"Groq {_GROQ_MODEL}"
                )

                # --- Groq (основная модель) fallback ---
                patterns, gq_reason = await _try_groq_batch(session, full_prompt)
                if patterns is not None:
                    model_stats["groq"] += 1
                else:
                    reasons["Groq"] = gq_reason or "unknown"
                    await _notify_fallback(
                        batch_num, "Groq", gq_reason or "unknown", _GROQ_FALLBACK_MODEL
                    )

                    # --- Groq fallback-модель (отдельная per-model квота) ---
                    patterns, scout_reason = await _try_groq_batch(
                        session, full_prompt, model=_GROQ_FALLBACK_MODEL
                    )
                    if patterns is not None:
                        model_stats["scout"] += 1
                    else:
                        reasons["Scout"] = scout_reason or "unknown"

            if patterns is None:
                # All 3 models failed for this batch — don't mark as analyzed.
                skipped += len(batch)
                model_stats["failed"] += 1
                consecutive_all_failed += 1
                current_delay = _BACKOFF_DELAY
                await _notify_all_failed(batch_num, reasons)
                if consecutive_all_failed >= _CIRCUIT_BREAKER_THRESHOLD:
                    try:
                        await message.answer(
                            f"🛑 Прерываю /aianalyze: {_CIRCUIT_BREAKER_THRESHOLD} батча подряд "
                            "не прошли ни через одну модель (похоже, дневные лимиты исчерпаны). "
                            "Попробуйте позже — квоты обычно сбрасываются в 00:00 UTC."
                        )
                    except Exception as exc:
                        logger.debug("aianalyze: circuit-breaker notice failed: %s", exc)
                    break
            else:
                consecutive_all_failed = 0
                current_delay = _RATE_DELAY
                for p in patterns:
                    eq = normalize_equipment(p.get("equipment"))
                    pt = (p.get("problem_type") or "").strip()
                    st = (p.get("steps") or "").strip()
                    if not pt or not st:
                        continue
                    if pattern_similar_in_index(pattern_index, eq, pt):
                        skipped += 1
                        continue
                    await save_solution_pattern(
                        problem_type=pt, steps=st, source="analyze", equipment=eq
                    )
                    pattern_index.setdefault(eq, []).append(pt.lower())
                    created += 1
                # Mark these items as analyzed so they're skipped next run
                await mark_knowledge_items_analyzed(batch_ids)

            # Adaptive rate-limit pause: bumped to _BACKOFF_DELAY after all-failed batch.
            await _asyncio.sleep(current_delay)

            # Update progress every 50 items
            processed = min(i + batch_size, len(items))
            if processed % 50 == 0 or processed == len(items):
                try:
                    await status_msg.edit_text(
                        f"⏳ Обработано {processed}/{len(items)} тикетов... "
                        f"Создано паттернов: {created}"
                    )
                except Exception as exc:
                    logger.debug("aianalyze: progress edit failed: %s", exc)

    elapsed = int(_time.monotonic() - t_start)
    counts = await count_solution_patterns_by_equipment()
    lines = [f"• {eq} — {cnt} паттернов" for eq, cnt in counts.items()]
    total_batches = sum(model_stats.values())
    model_breakdown_lines = [
        f"• Gemma 4 31B (OpenRouter): {model_stats['gemma4']}/{total_batches} батчей",
        f"• Groq {_GROQ_MODEL}: {model_stats['groq']}/{total_batches} батчей",
        f"• Groq {_GROQ_FALLBACK_MODEL}: {model_stats['scout']}/{total_batches} батчей",
        f"• Упали все 3: {model_stats['failed']}/{total_batches} батчей",
    ]
    report = (
        f"✅ Анализ завершён за {elapsed} сек.\n"
        f"Обработано тикетов: {len(items)}\n"
        f"Создано паттернов: {created}\n"
        f"Пропущено (дубли/ошибки): {skipped}\n\n"
        "🤖 Распределение по моделям:\n" + "\n".join(model_breakdown_lines) + "\n\n"
        "По оборудованию:\n" + "\n".join(lines) + "\n\n"
        "Запусти /aianalyze снова чтобы обновить."
    )
    try:
        await status_msg.edit_text(report)
    except Exception:
        await message.answer(report)
