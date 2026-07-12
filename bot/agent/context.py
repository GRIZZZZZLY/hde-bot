"""Сборка контекста агента: структурный бюджет истории (без разрезов посередине
сообщения) и evidence-retrieval со score/id/фрагментами для self-check и трассировки."""
from __future__ import annotations

from .actions import extract_client_text

_EXCERPT_LIMIT = 600


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

    history = build_history_budgeted(posts, info, _history_fn=_history_fn)
    client_text = extract_client_text(posts, getattr(info, "client_id", ""))
    equipment = _equipment_fn(ticket_title, history)
    retrieval_query = f"{ticket_title}\n{client_text}"[:600]

    evidence: list[dict] = []   # grounding-eligible: history + knowledge sources
    demos: list[dict] = []      # few-shot примеры (dialogue_pair): стиль, НЕ grounding
    confidence = 0
    embedding = await _embed_fn(retrieval_query, task_type="query")
    if embedding is not None:
        results = await _similar_fn(
            embedding, limit=3, query_text=retrieval_query, company_id=company_id
        )
        rank = 0
        for item, score in results:
            if score < RAG_MIN_SCORE:
                continue
            rank += 1
            evidence.append({
                "source_type": "knowledge_item",
                "source_id": item.id,
                "rank": rank,
                "score": round(float(score), 4),
                "title": getattr(item, "title", None),
                "used_excerpt": (item.content or "")[:_EXCERPT_LIMIT],
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

    from ..config import config
    if config.agent_dynamic_fewshot_enabled and embedding is not None:
        if _pairs_fn is None:
            from .pair_retrieval import find_similar_pairs as _pairs_fn
        try:
            pair_hits = await _pairs_fn(
                embedding, limit=3,
                exclude_ticket_ids={str(ticket_id)} if ticket_id else frozenset(),
                own_operator_id=str(config.hde_owner_id),
            )
            for hit in pair_hits:
                client_lines = [
                    ln for ln in hit["context"].splitlines() if ln.startswith("Клиент:")
                ]
                last_client = client_lines[-1][len("Клиент:"):].strip() if client_lines else ""
                # I3: пары идут в demos (few-shot), не в evidence (grounding)
                demos.append({
                    "source_type": "dialogue_pair",
                    "source_id": hit["pair_id"],
                    "rank": len(demos) + 1,
                    "score": hit["score"],
                    "title": f"тикет {hit['ticket_id']}",
                    "used_excerpt": (
                        f"Вопрос: {last_client}\n"
                        f"Ответ оператора: {hit['operator_answer'][:400]}"
                    ),
                })
        except Exception:
            pass  # few-shot не должен ронять генерацию

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
        "confidence": confidence,
    }
