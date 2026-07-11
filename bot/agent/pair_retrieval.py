"""Поиск похожих dialogue_pairs для dynamic few-shot (Phase 2B).

Косинус по problem-side эмбеддингам; порог обязателен (слабые совпадения не
подмешиваются); пары текущего тикета/golden set исключаются; двухпроходный
приоритет собственных ответов оператора (учимся у НЕГО, чужие — fallback)."""
from __future__ import annotations

import time

import numpy as np

PAIR_MIN_SCORE = 0.83
_CACHE_TTL = 300.0

_cache_rows: list[dict] | None = None
_cache_at: float = 0.0


def invalidate_pairs_cache() -> None:
    global _cache_rows, _cache_at
    _cache_rows = None
    _cache_at = 0.0


async def _load_candidates(_candidates_fn):
    global _cache_rows, _cache_at
    now = time.monotonic()
    if _cache_rows is not None and now - _cache_at < _CACHE_TTL:
        return _cache_rows
    if _candidates_fn is None:
        from ..db import list_fewshot_candidates as _candidates_fn
    _cache_rows = await _candidates_fn()
    _cache_at = now
    return _cache_rows


async def find_similar_pairs(
    query_embedding,
    *,
    limit: int = 3,
    exclude_ticket_ids=frozenset(),
    own_operator_id: str = "",
    _candidates_fn=None,
) -> list[dict]:
    rows = await _load_candidates(_candidates_fn)
    if not rows:
        return []
    query = np.asarray(query_embedding, dtype=np.float32)
    qn = np.linalg.norm(query)
    if not qn:
        return []
    query = query / qn
    excluded = {str(t) for t in exclude_ticket_ids}

    scored: list[dict] = []
    for row in rows:
        if str(row["ticket_id"]) in excluded:
            continue
        emb = np.frombuffer(row["embedding"], dtype=np.float32)
        if emb.shape != query.shape:
            continue
        score = float(np.dot(emb, query))  # кандидаты нормализованы при эмбеддинге
        if score < PAIR_MIN_SCORE:
            continue
        scored.append({
            "pair_id": row["pair_id"], "ticket_id": row["ticket_id"],
            "operator_user_id": row["operator_user_id"],
            "context": row["context"], "operator_answer": row["operator_answer"],
            "score": round(score, 4),
        })
    scored.sort(key=lambda r: r["score"], reverse=True)

    own = [r for r in scored if str(r["operator_user_id"]) == str(own_operator_id)]
    others = [r for r in scored if str(r["operator_user_id"]) != str(own_operator_id)]
    return (own + others)[:limit]
