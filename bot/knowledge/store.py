"""Vector store: save and retrieve knowledge items by cosine similarity."""
from __future__ import annotations

import logging

import numpy as np

from ..db import (
    KnowledgeItem,
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
) -> list[KnowledgeItem]:
    """Return top-N knowledge items most similar to query_embedding."""
    rows = await list_all_knowledge_embeddings()
    if not rows:
        return []

    scored: list[tuple[float, tuple[int, str]]] = []
    for row_id, content, emb_bytes in rows:
        try:
            emb = bytes_to_embedding(emb_bytes)
        except Exception:
            logger.warning("Skipping corrupted embedding row_id=%s", row_id, exc_info=True)
            continue
        score = cosine_similarity(query_embedding, emb)
        scored.append((score, (row_id, content)))

    scored.sort(key=lambda x: x[0], reverse=True)

    result = []
    for score, (row_id, content) in scored[:limit]:
        result.append(KnowledgeItem(
            id=row_id,
            source="",
            ticket_id=None,
            title=None,
            content=content,
            quality="good",
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
    )
    return item_id
