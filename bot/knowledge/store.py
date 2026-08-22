"""Vector store: save and retrieve knowledge items by cosine similarity."""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from ..db import (
    KnowledgeItem,
    fts_search_knowledge,
    list_all_knowledge_embeddings,
    save_knowledge_item,
)

logger = logging.getLogger(__name__)

# --- In-memory embedding cache (Priority 3) ---
# Reloads all embeddings from SQLite at most once per _CACHE_TTL seconds.
# Invariant: cache holds pre-parsed np.ndarray — avoids np.frombuffer on every query.
_CACHE_TTL = 60.0
_cache_rows: list[tuple[int, str, np.ndarray, str, str]] | None = None
_cache_loaded_at: float = 0.0
_cache_lock = asyncio.Lock()


async def _load_embeddings_cached() -> list[tuple[int, str, np.ndarray, str, str]]:
    """Return cached (id, content, ndarray, company_id, source) rows; reload if stale."""
    global _cache_rows, _cache_loaded_at
    now = time.monotonic()
    if _cache_rows is not None and (now - _cache_loaded_at) < _CACHE_TTL:
        return _cache_rows

    async with _cache_lock:
        # Double-check after acquiring lock
        now = time.monotonic()
        if _cache_rows is not None and (now - _cache_loaded_at) < _CACHE_TTL:
            return _cache_rows

        raw_rows = await list_all_knowledge_embeddings()
        parsed: list[tuple[int, str, np.ndarray, str, str]] = []
        for row_id, content, emb_bytes, item_company_id, item_source in raw_rows:
            try:
                emb = bytes_to_embedding(emb_bytes)
            except Exception:
                logger.warning("Skipping corrupted embedding row_id=%s", row_id, exc_info=True)
                continue
            parsed.append((row_id, content, emb, item_company_id, item_source or ""))
        _cache_rows = parsed
        _cache_loaded_at = now
        return _cache_rows


def invalidate_embeddings_cache() -> None:
    """Force the next find_similar call to reload embeddings from SQLite.

    Call after bulk imports or when staleness matters more than TTL.
    """
    global _cache_rows, _cache_loaded_at
    _cache_rows = None
    _cache_loaded_at = 0.0


# Stacked scoring matrix derived from _cache_rows. Keyed by the rows object's
# identity: a cache reload produces a new list, which triggers a rebuild here.
_matrix_rows: list | None = None
_matrix: np.ndarray | None = None
_matrix_norms: np.ndarray | None = None
_matrix_dim: int | None = None
_matrix_kept: list | None = None


def _scoring_arrays(
    rows: list[tuple[int, str, np.ndarray, str, str]], dim: int
) -> tuple[list, np.ndarray, np.ndarray]:
    """(строки, матрица, нормы) — только по векторам размерности `dim`.

    Вектор чужой размерности (запись от другой модели эмбеддингов) роняет
    матричное умножение, а это ВЕСЬ RAG на всех тикетах, а не одна строка.
    Сравниваем с размерностью ЗАПРОСА, а не с константой: у прода это 1024, а
    синтетические векторы в тестах остаются работоспособными между собой.
    """
    global _matrix_rows, _matrix, _matrix_norms, _matrix_dim, _matrix_kept
    if _matrix_rows is not rows or _matrix_dim != dim:
        kept = [r for r in rows if r[2].shape[0] == dim]
        skipped = len(rows) - len(kept)
        if skipped:
            logger.warning(
                "Skipping %d knowledge item(s) whose embedding dimension != %d "
                "(другая модель эмбеддингов) — переиндексировать через /aireindex",
                skipped, dim,
            )
        _matrix_kept = kept
        _matrix = (
            np.vstack([r[2] for r in kept]) if kept
            else np.zeros((0, dim), dtype=np.float32)
        )
        _matrix_norms = np.linalg.norm(_matrix, axis=1)
        _matrix_rows = rows
        _matrix_dim = dim
    return _matrix_kept, _matrix, _matrix_norms  # type: ignore[return-value]


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
    sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
) -> list[tuple[KnowledgeItem, float]]:
    """Return top-N (item, cosine_score) tuples.

    When query_text provided: merges cosine + BM25 via RRF.
    When company_id provided: same-company items get an extra RRF boost.
    When sources/exclude_sources given: ищем только в этих источниках. Нужно,
    чтобы чанки внутренней БЗ и примеры закрытых тикетов не делили одни слоты —
    статья и похожий тикет попадают в промпт как разные блоки.
    """
    rows = await _load_embeddings_cached()
    if not rows:
        return []

    # Cosine scoring: one matrix multiply over the cached embeddings, then
    # top limit*3 via argpartition instead of sorting the whole table.
    query32 = query_embedding.astype(np.float32, copy=False)
    rows, matrix, row_norms = _scoring_arrays(rows, int(query32.shape[0]))
    if not rows:
        return []
    q_norm = float(np.linalg.norm(query32))
    if q_norm < 1e-8:
        scores = np.zeros(len(rows), dtype=np.float32)
    else:
        denom = row_norms * q_norm
        scores = np.where(denom < 1e-8, 0.0, (matrix @ query32) / np.maximum(denom, 1e-12))

    allowed_ids: set[int] | None = None
    if sources is not None or exclude_sources is not None:
        mask = np.array(
            [
                (sources is None or r[4] in sources)
                and (exclude_sources is None or r[4] not in exclude_sources)
                for r in rows
            ]
        )
        if not mask.any():
            return []
        allowed_ids = {rows[i][0] for i in np.flatnonzero(mask)}
        # -1.0 ниже любого косинуса, поэтому отфильтрованные строки не попадут
        # в пул даже когда его размер равен размеру таблицы.
        scores = np.where(mask, scores, -1.0)
        limit = min(limit, int(mask.sum()))

    pool_size = min(limit * 3, len(rows))
    if pool_size < len(rows):
        top_idx = np.argpartition(-scores, pool_size - 1)[:pool_size]
    else:
        top_idx = np.arange(len(rows))
    # Stable sort keeps row order on ties — same tie-breaking as the old
    # full list.sort over rows in cache order.
    top_idx = top_idx[np.argsort(-scores[top_idx], kind="stable")]

    k = 60  # RRF constant
    rrf_scores: dict[int, float] = {}
    contents: dict[int, str] = {}
    cosine_top: dict[int, float] = {}

    # Отсеянные источники помечены -1.0 и могли попасть в пул, если разрешённых
    # строк меньше его размера — из результата их надо убрать.
    pool = [
        (float(scores[i]), rows[i][0], rows[i][1], rows[i][3])
        for i in top_idx
        if scores[i] > -0.5
    ]

    for rank, (score, item_id, content, _) in enumerate(pool):
        rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
        contents[item_id] = content
        cosine_top[item_id] = score

    if query_text:
        bm25_rows = await fts_search_knowledge(query_text, limit=limit * 3)
        if bm25_rows:
            # BM25-находки вне cosine-пула получают реальный cosine-score,
            # иначе порог уверенности в get_rag_context убьёт точные
            # совпадения по ключевым словам (коды ошибок, модели касс).
            id_to_idx = {rows[i][0]: i for i in range(len(rows))}
            for rank, (item_id, content) in enumerate(bm25_rows):
                if allowed_ids is not None and item_id not in allowed_ids:
                    continue
                rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
                contents[item_id] = content
                if item_id not in cosine_top:
                    idx = id_to_idx.get(item_id)
                    if idx is not None:
                        cosine_top[item_id] = float(scores[idx])

    # Company boost: same-company items get bonus equivalent to rank-1 position
    if company_id:
        for _, item_id, _, item_company_id in pool:
            if item_company_id == company_id and item_id in rrf_scores:
                rrf_scores[item_id] += 1.0 / (k + 1)

    sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)
    result = []
    seen_contents: set[str] = set()
    for item_id in sorted_ids:
        if len(result) >= limit:
            break
        # КБ содержит буквальные дубли под разными id (один диалог,
        # разные source) — копия не должна сжигать слот в top-k.
        norm = " ".join(contents[item_id].split()).lower()
        if norm in seen_contents:
            continue
        seen_contents.add(norm)
        score = cosine_top.get(item_id, 0.0)
        result.append((
            KnowledgeItem(id=item_id, source="", ticket_id=None, title=None,
                          content=contents[item_id], quality="good"),
            score,
        ))

    # title/url одним запросом: ссылка на статью должна доехать до промпта,
    # чтобы бот мог ответить ею клиенту (как это делают операторы).
    if result:
        ids = [item.id for item, _ in result]
        try:
            from .. import db as _db
            async with _db.connect() as conn:
                placeholders = ",".join("?" * len(ids))
                async with conn.execute(
                    f"SELECT id, title, url FROM knowledge_items "
                    f"WHERE id IN ({placeholders})", ids,
                ) as cur:
                    meta = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
            for item, _ in result:
                title, url = meta.get(item.id, (None, None))
                item.title, item.url = title, url
        except Exception as exc:
            logger.warning("knowledge meta fetch failed: %s", exc)

    # Update last_used_at for returned items (throttled 24h, non-fatal)
    if result:
        returned_ids = [item.id for item, _ in result if item.id is not None]
        if returned_ids:
            try:
                from .. import db as _db
                await _db.update_knowledge_last_used(returned_ids)
            except Exception as exc:
                logger.warning("update_knowledge_last_used failed: %s", exc)

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
    invalidate_embeddings_cache()
    return item_id
