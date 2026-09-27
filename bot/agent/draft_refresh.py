"""Пересборка черновика, когда коллега дописал внутренний комментарий.

Черновик рождается по первому сообщению клиента. Комментарий первой линии
(«звонил в банк», «у клиента Эвотор 7.3», «выезд назначен») появляется позже, и
на черновик не влиял: агент по тикету стартует один раз.

Отдельного вебхука на внутренний комментарий в HelpDeskEddy нет — HANDLERS в
bot/hde_webhook.py знает только client_reply / staff_reply / ticket_updated,
поэтому событием это не поймать без нового правила диспетчера. Джоб замечает
такие комментарии сам, опрашивая тикеты со свежими неотправленными черновиками.

Ограничения жёсткие и все по одной причине: TPM у Groq free-tier 8000, и
половина тикетов уже сегодня режет историю по бюджету (8–15 записей «Prompt
budget… history trimmed» в сутки). Поэтому пересборка — одна на тикет, только
пока черновик не отправлен, и не больше LIMIT тикетов за проход.

Ядро чистое: выборка, опрос HDE, регенерация и сон приходят снаружи.
"""
from __future__ import annotations

import logging

from .dialogue_mining import is_staff_post, sort_posts
from .operator_text import strip_html

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 3
DEFAULT_HOURS = 6
DEFAULT_PAUSE_S = 4.0


def _post_id(post) -> int:
    try:
        return int(getattr(post, "post_id", 0))
    except (TypeError, ValueError):
        return 0


def find_new_colleague_comment(posts, anchor_post_id, staff: set[str]) -> str | None:
    """Текст внутреннего комментария коллеги, появившегося после якоря.

    Только is_comment: публичный пост клиента поводом не считается — под ним уже
    висит кнопка «💡 Предложить ответ», и звать модель решает оператор.
    """
    try:
        anchor = int(anchor_post_id)
    except (TypeError, ValueError):
        anchor = 0
    texts = []
    for post in sort_posts(posts):
        if _post_id(post) <= anchor:
            continue
        if not getattr(post, "is_comment", False):
            continue
        if not is_staff_post(post, staff):
            continue
        text = strip_html(getattr(post, "text", ""))
        if text:
            texts.append(text)
    return "\n".join(texts) if texts else None


async def refresh_stale_drafts(
    bot=None, *, hours: int = DEFAULT_HOURS, limit: int = DEFAULT_LIMIT,
    pause_s: float = DEFAULT_PAUSE_S,
    _stale_fn=None, _comments_fn=None, _refreshed_fn=None, _regen_fn=None,
    _sleep_fn=None, _staff=None,
) -> dict:
    """Один проход: найти черновики, которые устарели из-за комментария коллеги.

    Ошибка по одному тикету не роняет проход — HDE отдаёт 503 регулярно, и
    остальные тикеты за это платить не должны.
    """
    from ..config import config

    stats = {"refreshed": 0, "skipped": 0, "errors": 0}
    if not getattr(config, "agent_draft_refresh_enabled", False):
        return stats

    if _stale_fn is None:
        from ..db import list_stale_drafts as _stale_fn
    if _refreshed_fn is None:
        from ..db import ticket_draft_refreshed as _refreshed_fn
    if _regen_fn is None:
        async def _regen_fn(*, ticket_id, topic_id, reason):
            return await regenerate_draft(
                bot, ticket_id=ticket_id, topic_id=topic_id, reason=reason
            )
    if _sleep_fn is None:
        import asyncio
        _sleep_fn = asyncio.sleep
    if _comments_fn is None:
        from ..hde_api import HDEApiClient
        _client = HDEApiClient()

        async def _comments_fn(ticket_id):
            return await _client.get_ticket_comments(ticket_id)

    staff = set(_staff) if _staff is not None else {
        str(config.hde_owner_id), *config.agent_staff_user_ids
    }

    for i, sug in enumerate(await _stale_fn(hours=hours, limit=limit)):
        ticket_id = str(sug["ticket_id"])
        try:
            if await _refreshed_fn(ticket_id):
                stats["skipped"] += 1
                continue
            comments = await _comments_fn(ticket_id)
            reason = find_new_colleague_comment(
                comments, sug.get("context_until_post_id"), staff
            )
            if not reason:
                continue
            if i and pause_s:
                await _sleep_fn(pause_s)
            result = await _regen_fn(
                ticket_id=ticket_id, topic_id=sug.get("topic_id"), reason=reason,
            )
            if result is None:
                stats["errors"] += 1
                continue
            stats["refreshed"] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("draft refresh: ticket %s failed: %s", ticket_id, exc)
    return stats


async def regenerate_draft(bot, *, ticket_id: str, topic_id, reason: str):
    """Перегенерировать черновик и запостить его в топик как обновление.

    Тот же путь, что у кнопки «💡 Предложить ответ», только trigger_source
    другой: по нему ночная сверка отличает пересобранный черновик, а
    ticket_draft_refreshed — что по тикету пересборка уже была.
    """
    from .. import topic_manager as _tm
    from ..handlers.ai_feedback import post_suggestion_messages
    from ..hde_api import HDEApiClient, HDEApiError, post_sort_key

    if topic_id is None or bot is None:
        return None
    client = HDEApiClient()
    info = await client.get_ticket_info(ticket_id)
    posts = await client.get_ticket_posts(ticket_id)
    try:
        comments = await client.get_ticket_comments(ticket_id)
    except HDEApiError:
        comments = []
    all_posts = sorted(posts + comments, key=post_sort_key)
    anchor = str(max((p.post_id for p in all_posts), default="")) or None

    record = await _tm.db.get_topic(ticket_id)
    ticket_title = (record.ticket_name if record else "") or ""
    result = await _tm._generate_summary_with_retry(
        all_posts, info,
        ticket_title=ticket_title,
        ticket_id=ticket_id,
        company_id="",
        topic_id=topic_id,
        trigger_source="comment",
    )
    if result is None:
        return None
    suit_line, client_line, memo_line, confidence_pct = result
    try:
        await bot.send_message(
            chat_id=_tm.config.group_chat_id,
            message_thread_id=topic_id,
            text="🔄 Черновик обновлён: коллега дописал комментарий в тикете.",
        )
    except Exception as exc:
        logger.debug("draft refresh: notice not delivered for %s: %s", ticket_id, exc)
    await post_suggestion_messages(
        bot,
        topic_id=topic_id,
        ticket_id=ticket_id,
        suit_line=suit_line,
        client_line=client_line,
        memo_line=memo_line,
        confidence_pct=confidence_pct,
        all_posts=all_posts,
        info=info,
        ticket_title=ticket_title,
        anchor=anchor,
        trigger_source="comment",
    )
    return True
