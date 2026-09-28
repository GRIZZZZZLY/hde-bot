"""Сборка контекста агента: структурный бюджет истории (без разрезов посередине
сообщения) и evidence-retrieval со score/id/фрагментами для self-check и трассировки."""
from __future__ import annotations

import logging

from .actions import extract_client_text

logger = logging.getLogger(__name__)

_EXCERPT_LIMIT = 600
_ATTACHMENT_LIMIT = 900
_CALL_NOTES_LIMIT = 900
_V2_HISTORY_CAP = 3000      # бюджет режет целыми сообщениями, одно огромное проходит целиком


def _excerpt_with_url(item) -> str:
    """Фрагмент знания для промпта; URL статьи первым — модель может отдать
    его клиенту (операторы часто решают тикет именно ссылкой)."""
    excerpt = (item.content or "")[:_EXCERPT_LIMIT]
    url = getattr(item, "url", None)
    return f"Статья: {url}\n{excerpt}" if url else excerpt


def build_ticket_facts(topic) -> str:
    """Строка «что мы про этот тикет уже знаем» из полей, которые бот сам заполнил.

    Только имена, не id: «14» модель не расшифрует. Пустое значение окружения —
    это «классифицировали и не определили», и в промпте оно вреднее молчания:
    модель прочитает «не определено» как факт о клиенте.
    """
    if topic is None:
        return ""
    from ..ticket_fields import OKRUZHENIE_OPTIONS

    facts: list[str] = []
    company = (getattr(topic, "company_name", "") or "").strip()
    if company:
        facts.append(f"Компания клиента: {company}")
    env_id = getattr(topic, "env_option_id", None)
    if env_id and str(env_id) in OKRUZHENIE_OPTIONS:
        facts.append(f"Окружение: {OKRUZHENIE_OPTIONS[str(env_id)]}")
    return "\n".join(facts)


async def load_ticket_side_context(ticket_id: str, *, _topic_fn=None) -> dict:
    """Описания вложений, заметки после звонка и поля тикета — из ticket_topics.

    Всё это бот собирает сам (Vision по вложениям, Deepgram по звонку,
    автозаполнение полей), но до черновика ничего из этого не доезжало: читали
    только автозаполнение и индексатор базы знаний. Отсюда «уточните, какая
    ошибка на экране» под присланным скриншотом.

    Промах по топику (удалён, старый тикет, сбой базы) — пустой контекст, а не
    падение: черновик без описания вложения хуже, чем с ним, но лучше, чем ничего.
    """
    if not ticket_id:
        return {"attachments": "", "call_notes": "", "ticket_facts": ""}
    if _topic_fn is None:
        from ..db import get_topic as _topic_fn
    try:
        topic = await _topic_fn(str(ticket_id))
    except Exception as exc:
        logger.debug("agent context: topic lookup failed for %s: %s", ticket_id, exc)
        return {"attachments": "", "call_notes": "", "ticket_facts": ""}
    if topic is None:
        return {"attachments": "", "call_notes": "", "ticket_facts": ""}
    return {
        "attachments": (
            getattr(topic, "photo_descriptions", "") or ""
        ).strip()[:_ATTACHMENT_LIMIT],
        "call_notes": (
            getattr(topic, "call_notes", "") or ""
        ).strip()[:_CALL_NOTES_LIMIT],
        "ticket_facts": build_ticket_facts(topic),
    }


def build_history_budgeted(posts, info, *, budget: int = 3000, _history_fn=None) -> str:
    if _history_fn is None:
        from ..ai_summary import _build_history_text as _history_fn
    posts = list(posts)
    full = _history_fn(posts, info)
    if len(full) <= budget or len(posts) <= 2:
        return full
    client_id = getattr(info, "client_id", "")
    first_idx = next(
        (i for i, p in enumerate(posts) if str(p.user_id) == str(client_id)), 0
    )
    head = _history_fn([posts[first_idx]], info)
    # хвост целыми сообщениями, сколько влезает в остаток бюджета
    remaining = budget - len(head)
    tail_posts: list = []
    for post in reversed(posts[first_idx + 1:]):
        candidate = _history_fn([post] + tail_posts, info)
        if len(candidate) > remaining:
            break
        tail_posts.insert(0, post)
    skipped = len(posts) - 1 - len(tail_posts)
    marker = f"\n[...пропущено {max(skipped, 0)} сообщений...]\n" if skipped > 0 else "\n"
    tail = _history_fn(tail_posts, info) if tail_posts else ""
    return head + marker + tail


async def build_agent_context(
    posts,
    info,
    ticket_title: str,
    company_id: str = "",
    *,
    ticket_id: str = "",
    _history_fn=None,
    _embed_fn=None,
    _similar_fn=None,
    _equipment_fn=None,
    _wiki_fn=None,
    _pattern_fn=None,
    _pairs_fn=None,
    _topic_fn=None,
) -> dict:
    if _embed_fn is None:
        from ..knowledge.indexer import embed_text as _embed_fn
    if _similar_fn is None:
        from ..knowledge.store import find_similar as _similar_fn
    if _equipment_fn is None:
        from ..ai_summary import _detect_equipment as _equipment_fn
    if _wiki_fn is None:
        from ..wiki.searcher import get_wiki_context as _wiki_fn
    if _pattern_fn is None:
        from ..db import find_solution_pattern as _pattern_fn
    from ..knowledge.indexer import RAG_MIN_SCORE
    from ..config import config

    history = build_history_budgeted(posts, info, _history_fn=_history_fn)
    client_text = extract_client_text(posts, getattr(info, "client_id", ""))
    equipment = _equipment_fn(ticket_title, history)
    v2 = config.agent_voice_v2_enabled
    if v2:
        from . import context_v2 as cv2
        messages = cv2.client_messages(posts, getattr(info, "client_id", ""))
        history = cv2.strip_history_macros(history)
        if len(history) > _V2_HISTORY_CAP:
            history = history[:1000] + "\n[...часть переписки пропущена...]\n" + history[-2000:]
        retrieval_query = cv2.build_retrieval_query(ticket_title, messages)
    else:
        retrieval_query = f"{ticket_title}\n{client_text}"[:600]
    side = await load_ticket_side_context(ticket_id, _topic_fn=_topic_fn)

    evidence: list[dict] = []   # grounding-eligible: history + knowledge sources
    demos: list[dict] = []      # few-shot примеры (dialogue_pair): стиль, НЕ grounding
    confidence = 0
    embedding = await _embed_fn(retrieval_query, task_type="query")
    if embedding is not None:
        results = await _similar_fn(
            embedding, limit=2 if v2 else 3, query_text=retrieval_query, company_id=company_id
        )
        rank = 0
        for item, score in results:
            if score < RAG_MIN_SCORE:
                continue
            rank += 1
            excerpt = (
                cv2.best_chunk(item.content or "", retrieval_query)
                if v2 else _excerpt_with_url(item)
            )
            if v2:
                url = getattr(item, "url", None)
                excerpt = f"Статья: {url}\n{excerpt}" if url else excerpt
            evidence.append({
                "source_type": "knowledge_item",
                "source_id": item.id,
                "rank": rank,
                "score": round(float(score), 4),
                "title": getattr(item, "title", None),
                "used_excerpt": excerpt,
            })
            confidence = max(confidence, int(round(float(score) * 100)))

    wiki = await _wiki_fn(ticket_title)
    if wiki:
        evidence.append({
            "source_type": "wiki", "source_id": None,
            "rank": len(evidence) + 1, "score": None,
            "title": ticket_title[:80], "used_excerpt": wiki[:_EXCERPT_LIMIT],
        })
    pattern = await _pattern_fn(equipment, ticket_title)
    solution_steps = pattern.get("steps") if pattern else None
    if solution_steps:
        evidence.append({
            "source_type": "solution_pattern", "source_id": pattern.get("id"),
            "rank": len(evidence) + 1, "score": None,
            "title": equipment or "", "used_excerpt": solution_steps[:_EXCERPT_LIMIT],
        })

    if config.agent_dynamic_fewshot_enabled and embedding is not None:
        if _pairs_fn is None:
            from .pair_retrieval import find_similar_pairs as _pairs_fn
        try:
            pair_hits = await _pairs_fn(
                embedding, limit=2 if v2 else 3,
                exclude_ticket_ids={str(ticket_id)} if ticket_id else frozenset(),
                own_operator_id=str(config.hde_owner_id),
            )
            for hit in pair_hits:
                client_lines = [
                    ln for ln in hit["context"].splitlines() if ln.startswith("Клиент:")
                ]
                last_client = client_lines[-1][len("Клиент:"):].strip() if client_lines else ""
                answer = (
                    cv2.clean_demo_answer(hit["operator_answer"])[:300]
                    if v2 else hit["operator_answer"][:400]
                )
                # I3: пары идут в demos (few-shot), не в evidence (grounding)
                demos.append({
                    "source_type": "dialogue_pair",
                    "source_id": hit["pair_id"],
                    "rank": len(demos) + 1,
                    "score": hit["score"],
                    "title": f"тикет {hit['ticket_id']}",
                    "used_excerpt": (
                        f"Вопрос: {last_client}\n"
                        f"Ответ оператора: {answer}"
                    ),
                })
        except Exception:
            pass  # few-shot не должен ронять генерацию

    # Отметка «этой статьёй пользовались». Без неё архивация автоправил (см.
    # db.archive_unused_auto_rules) сносила бы и те, что исправно работают:
    # last_used_at до этой правки писал только legacy-путь суммарки.
    used_ids = [
        e["source_id"] for e in evidence
        if e["source_type"] == "knowledge_item" and e.get("source_id")
    ]
    if used_ids:
        try:
            from ..db import update_knowledge_last_used
            await update_knowledge_last_used(used_ids)
        except Exception as exc:
            logger.debug("agent context: last_used_at not updated: %s", exc)

    grounds = []
    for e in evidence:
        if e["source_type"] == "knowledge_item":
            grounds.append(f"KB#{e['source_id']}")
        elif e["source_type"] == "wiki":
            grounds.append(f"wiki:{(e['title'] or '')[:40]}")
        else:
            grounds.append(f"pattern:{e['title'] or '?'}")
    for d in demos:
        grounds.append(f"пара#{d['source_id']}")

    return {
        "history": history,
        "client_text": client_text,
        "equipment": equipment,
        "evidence": evidence,
        "demos": demos,
        "retrieval_query": retrieval_query,
        "wiki": wiki,
        "solution_steps": solution_steps,
        "grounds": grounds,
        "stress": cv2.detect_stress(messages) if v2 else False,
        "ticket_state": (
            cv2.ticket_state(ticket_title, messages,
                             cv2.staff_messages(posts, getattr(info, "client_id", "")))
            if v2 else ""
        ),
        "first_staff_reply": (
            cv2.is_first_staff_reply(posts, getattr(info, "client_id", "")) if v2 else False
        ),
        "confidence": confidence,
        # Факты о тикете, а не источники: в evidence им не место — self-check
        # стал бы требовать опоры на пересказ картинки от Vision.
        "attachments": side["attachments"],
        "call_notes": side["call_notes"],
        "ticket_facts": side["ticket_facts"],
    }
