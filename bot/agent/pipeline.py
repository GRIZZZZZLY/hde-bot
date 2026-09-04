# bot/agent/pipeline.py
"""Оркестратор агентного пайплайна (Phase 1, rev.2).

Порядок: дешёвый pre-check → контекст+retrieval → драфт → post-safety →
self-check (evidence) → freshness (одна перегенерация) → Памятка → запись."""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)


def _anchor_post_id(posts) -> str | None:
    ids = [getattr(p, "post_id", None) for p in posts]
    ids = [int(i) for i in ids if i is not None]
    return str(max(ids)) if ids else None


async def _default_posts_fn(ticket_id: str):
    from ..hde_api import HDEApiClient
    client = HDEApiClient()
    posts = await client.get_ticket_posts(ticket_id)
    comments = await client.get_ticket_comments(ticket_id)
    return list(posts) + list(comments)


async def run_agent(
    posts,
    info,
    *,
    ticket_title: str,
    ticket_id: str,
    topic_id: int | None = None,
    company_id: str = "",
    trigger_source: str = "first",
    _context_fn=None,
    _draft_fn=None,
    _selfcheck_fn=None,
    _safety_pre=None,
    _safety_post=None,
    _record_fn=None,
    _posts_fn=None,
) -> tuple[str, str, str, int] | None:
    if _context_fn is None:
        from .context import build_agent_context as _context_fn
    if _draft_fn is None:
        from .generate import generate_agent_draft as _draft_fn
    if _selfcheck_fn is None:
        from .selfcheck import self_check as _selfcheck_fn
    if _safety_pre is None:
        from .safety import pre_generation_policy_check as _safety_pre
    if _safety_post is None:
        from .safety import post_generation_safety_check as _safety_post
    if _record_fn is None:
        from ..db import record_suggestion as _record_fn
    if _posts_fn is None:
        _posts_fn = _default_posts_fn

    from .actions import compose_memo, compose_selfcheck_warning, extract_client_text

    started = time.monotonic()
    posts = list(posts)
    original_anchor = _anchor_post_id(posts)
    client_text_quick = extract_client_text(posts, getattr(info, "client_id", ""))

    # 1-2. Дешёвый policy pre-check ДО retrieval
    pre = _safety_pre(f"{ticket_title}\n{client_text_quick}")
    if pre.action == "ESCALATE":
        memo = compose_memo(
            f"⚠️ Эскалация ({pre.category}): вопрос требует оператора."
        )
        await _record_nonfatal(
            _record_fn, anchor=original_anchor, info=info, ticket_id=ticket_id,
            topic_id=topic_id, trigger_source=trigger_source,
            ticket_title=ticket_title, history="",
            client_text=client_text_quick, client="", suit="", memo=memo,
            action="ESCALATE", self_status="n/a", evidence=[],
            retrieval_query=None, confidence=0,
            confidence_reason=f"policy pre-check: {pre.category}",
            started=started,
        )
        return "", "", memo, 0

    # 3-6. Полный проход; при superseded — одна перегенерация
    stale_warning = False
    context = draft = None
    action = suit = client = base_memo = ""
    draft_client = ""
    confidence = 0
    confidence_reason = ""
    self_status = "n/a"
    for attempt in range(2):
        context = await _context_fn(posts, info, ticket_title, company_id, ticket_id=ticket_id)
        draft = await _draft_fn(context, ticket_title)
        if draft is None:
            return None  # драфт не удался → откат на legacy
        action = draft["action"]
        suit, client, base_memo = draft["suit"], draft["client"], draft["memo"]
        draft_client = client
        confidence, confidence_reason = draft["confidence"], draft["confidence_reason"]
        self_status = "n/a"

        post_check = _safety_post(client)
        if post_check.action == "ESCALATE":
            action, client, confidence = "ESCALATE", "", 0
            base_memo = (f"⚠️ Эскалация ({post_check.category}): предложенный ответ "
                         f"небезопасен. " + base_memo)
        elif action == "ANSWER":
            check = await _selfcheck_fn(
                context["client_text"], client, context["evidence"], context["history"]
            )
            self_status = check["status"]
            if self_status == "partially_supported":
                # драфт живёт: пометка оператору вместо замены на шаблонный вопрос
                base_memo = ("⚠️ Часть ответа без опоры на источники — проверь факты. "
                             + base_memo)
                confidence = min(confidence, 50)
                confidence_reason = "self-check: partially_supported, драфт сохранён"
            elif self_status != "supported":
                # Драфт тоже живёт. Подмена его на fallback_client_text была
                # источником целого класса расхождений «эскалация вместо
                # решения»: сверка 2026-09 показала три тикета подряд, где
                # драфт совпадал с ответом оператора, а fallback предлагал
                # ждать специалиста. Клиенту ничего не уходит без кнопки
                # оператора — предупреждение в Памятке решает задачу без потери
                # готового ответа.
                base_memo = compose_selfcheck_warning(check, base_memo)
                confidence = min(confidence, 40)
                confidence_reason = (
                    "self-check не отработал, драфт сохранён"
                    if not check.get("checked", True)
                    else f"self-check: {self_status}, драфт сохранён"
                )

        # freshness: не появился ли новый пост, пока генерировали
        anchor = _anchor_post_id(posts)
        try:
            fresh_posts = list(await _posts_fn(ticket_id))
        except Exception as exc:
            logger.warning("run_agent: freshness fetch failed: %s", exc)
            fresh_posts = posts
        fresh_anchor = _anchor_post_id(fresh_posts)
        if fresh_anchor == anchor or attempt == 1:
            stale_warning = fresh_anchor != anchor
            break
        posts = fresh_posts  # superseded → одна перегенерация на свежих постах

    memo = compose_memo(base_memo, stale_warning=stale_warning)
    # трассировка сохраняет провенанс обоих контейнеров; self-check видел только evidence
    trace_refs = context["evidence"] + context.get("demos", [])
    await _record_nonfatal(
        _record_fn, anchor=original_anchor, info=info, ticket_id=ticket_id, topic_id=topic_id,
        trigger_source=trigger_source,
        ticket_title=ticket_title, history=context["history"],
        client_text=context["client_text"], client=client, suit=suit, memo=memo,
        draft_client=draft_client,
        action=action, self_status=self_status, evidence=trace_refs,
        retrieval_query=context["retrieval_query"], confidence=confidence,
        confidence_reason=confidence_reason, started=started,
    )
    return suit, client, memo, confidence


async def _record_nonfatal(
    record_fn, *, anchor, info, ticket_id, topic_id, trigger_source, ticket_title,
    history, client_text, client, suit, memo, action, self_status, evidence,
    retrieval_query, confidence, confidence_reason, started, draft_client="",
) -> None:
    try:
        from ..ai_summary import prompt_version_tag
        from ..config import config
        await record_fn(
            ticket_id=ticket_id,
            topic_id=topic_id,
            trigger_source=trigger_source,
            context_until_post_id=anchor,
            client_id=str(getattr(info, "client_id", "") or "") or None,
            pipeline_version=config.agent_pipeline_version,
            prompt_version=prompt_version_tag(),
            title=ticket_title,
            history=history,
            client_text=client_text,
            ai_answer=client,
            ai_full_text=f"{suit}\n{client}\n{memo}",
            draft_answer=draft_client,
            model=config.agent_draft_model,
            action_type=action,
            self_check=json.dumps({"status": self_status}, ensure_ascii=False),
            retrieved_refs=json.dumps(evidence, ensure_ascii=False),
            confidence=confidence,
            confidence_reason=confidence_reason,
            retrieval_query=retrieval_query,
            retrieval_config_version="v1",
            embedding_model="intfloat/multilingual-e5-large",
            generation_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        logger.warning("run_agent: suggestion record failed: %s", exc)
