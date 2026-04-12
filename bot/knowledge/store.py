"""Vector store: save and retrieve knowledge items by cosine similarity."""
from __future__ import annotations

import logging

import numpy as np

from ..db import (
    KnowledgeItem,
    fts_search_knowledge,
    list_all_knowledge_embeddings,
    save_knowledge_item,
)

logger = logging.getLogger(__name__)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Return cosine similarity in [-1, 1]. Handles zero vectors safely."""
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-8:
        return 0.0
    return float(np.dot(a, b) / denom)


def embedding_to_bytes(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32).copy()


async def find_similar(
    query_embedding: np.ndarray,
    *,
    limit: int = 3,
    query_text: str = "",
    company_id: str = "",
) -> list[tuple[KnowledgeItem, float]]:
    """Return top-N (item, cosine_score) tuples.

    When query_text provided: merges cosine + BM25 via RRF.
    When company_id provided: same-company items get an extra RRF boost.
    """
    rows = await list_all_knowledge_embeddings()
    if not rows:
        return []

    # Cosine scoring
    cosine_scored: list[tuple[float, int, str, str]] = []
    for row_id, content, emb_bytes, item_company_id in rows:
        try:
            emb = bytes_to_embedding(emb_bytes)
        except Exception:
            logger.warning("Skipping corrupted embedding row_id=%s", row_id, exc_info=True)
            continue
        score = cosine_similarity(query_embedding, emb)
        cosine_scored.append((score, row_id, content, item_company_id))
    cosine_scored.sort(key=lambda x: x[0], reverse=True)

    k = 60  # RRF constant
    rrf_scores: dict[int, float] = {}
    contents: dict[int, str] = {}
    cosine_top: dict[int, float] = {}

    pool = cosine_scored[: limit * 3]

    for rank, (score, item_id, content, _) in enumerate(pool):
        rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
        contents[item_id] = content
        cosine_top[item_id] = score

    if query_text:
        bm25_rows = await fts_search_knowledge(query_text, limit=limit * 3)
        for rank, (item_id, content) in enumerate(bm25_rows):
            rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
            contents[item_id] = content

    # Company boost: same-company items get bonus equivalent to rank-1 position
    if company_id:
        for _, item_id, _, item_company_id in pool:
            if item_company_id == company_id and item_id in rrf_scores:
                rrf_scores[item_id] += 1.0 / (k + 1)

    sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)
    result = []
    for item_id in sorted_ids[:limit]:
        score = cosine_top.get(item_id, 0.0)
        result.append((
            KnowledgeItem(id=item_id, source="", ticket_id=None, title=None,
                          content=contents[item_id], quality="good"),
            score,
        ))
    return result


async def save_and_index(
    source: str,
    content: str,
    embedding: np.ndarray,
    *,
    ticket_id: str = "",
    title: str = "",
    quality: str = "good",
    url: str = "",
    company_id: str = "",
    company_name: str = "",
) -> int:
    """Save content + embedding in one call. Returns new item id."""
    emb_bytes = embedding_to_bytes(embedding)
    item_id = await save_knowledge_item(
        source=source,
        content=content,
        ticket_id=ticket_id,
        title=title,
        embedding=emb_bytes,
        quality=quality,
        url=url,
        company_id=company_id,
        company_name=company_name,
    )
    return item_id
