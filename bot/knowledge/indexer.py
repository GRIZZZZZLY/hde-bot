"""Embed text via local sentence-transformers and index into knowledge store."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import numpy as np


from .. import db
from .store import embedding_to_bytes, find_similar

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_MODEL_NAME = "intfloat/multilingual-e5-large"
EMBEDDING_DIM = 1024  # output dimension of multilingual-e5-large

# --- Priority 4: noise cleaning before embedding ---
# Russian/English signatures — cut everything from the marker to the end.
_SIG_MARKERS_RE = re.compile(
    r"(?is)(?:^|\n)\s*(?:--\s*\n|с\s+уваж(?:ением|\.)|best\s+regards|kind\s+regards|"
    r"с\s+наилучшими|отправлено\s+с|sent\s+from\s+my).*$"
)
_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_QUOTED_LINE_RE = re.compile(r"(?m)^\s*>.*$")       # quoted email replies
_TICKET_ID_RE = re.compile(r"#\d{4,}")              # "#12345" — not useful for semantics
_MULTI_BLANK_RE = re.compile(r"\n\s*\n\s*\n+")
_INLINE_WS_RE = re.compile(r"[ \t]+")


def clean_for_embedding(text: str) -> str:
    """Strip signatures, URLs, emails, quoted replies, and excess whitespace.

    Raw content is still persisted in knowledge_items; only the embedding input
    is sanitised so cosine similarity focuses on problem semantics, not boilerplate.
    """
    if not text:
        return ""
    t = _SIG_MARKERS_RE.sub("", text)
    t = _QUOTED_LINE_RE.sub("", t)
    t = _URL_RE.sub("", t)
    t = _EMAIL_RE.sub("", t)
    t = _TICKET_ID_RE.sub("", t)
    t = _MULTI_BLANK_RE.sub("\n\n", t)
    t = _INLINE_WS_RE.sub(" ", t)
    return t.strip()

_model: "SentenceTransformer | None" = None
_model_lock = asyncio.Lock()


def _load_model() -> "SentenceTransformer":
    """Load the model synchronously (called in executor on first use)."""
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    global _model
    if _model is None:
        logger.info("Loading embedding model %s (first use, may take a moment)...", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
        logger.info("Embedding model loaded.")
    return _model


async def _get_model() -> "SentenceTransformer":
    """Return (lazily loaded) sentence-transformers model."""
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _load_model)
    return _model  # type: ignore[return-value]


async def embed_text(text: str, *, task_type: str = "passage") -> np.ndarray | None:
    """Return EMBEDDING_DIM-dim float32 embedding or None on failure.

    Args:
        text: Text to embed.
        task_type: "passage" for content being indexed, "query" for search queries.
                   multilingual-e5-large requires these prefixes for best quality.
    """
    prefix = "query: " if task_type == "query" else "passage: "
    input_text = f"{prefix}{text[:8000]}"
    try:
        model = await _get_model()
        loop = asyncio.get_event_loop()
        embedding: np.ndarray = await loop.run_in_executor(
            None,
            lambda: model.encode(input_text, normalize_embeddings=True),
        )
        return embedding.astype(np.float32)
    except Exception as exc:
        logger.warning("embed_text failed: %s", exc)
        return None


async def embed_texts(
    texts: list[str], *, task_type: str = "passage", batch_size: int = 32
) -> list[np.ndarray] | None:
    """Embed a list of texts in one batched model.encode call.

    Returns a list aligned with the input, or None if encoding failed.
    """
    prefix = "query: " if task_type == "query" else "passage: "
    inputs = [f"{prefix}{t[:8000]}" for t in texts]
    try:
        model = await _get_model()
        loop = asyncio.get_event_loop()
        embeddings: np.ndarray = await loop.run_in_executor(
            None,
            lambda: model.encode(
                inputs, batch_size=batch_size, normalize_embeddings=True
            ),
        )
        return [emb.astype(np.float32) for emb in embeddings]
    except Exception as exc:
        logger.warning("embed_texts failed: %s", exc)
        return None


async def index_knowledge_item(
    source: str,
    content: str,
    *,
    ticket_id: str = "",
    title: str = "",
    quality: str = "good",
    url: str = "",
    content_hash: str = "",
    company_id: str = "",
    company_name: str = "",
) -> int | None:
    """Embed content and upsert into knowledge_items. Returns item id or None.

    If ticket has stored Vision photo descriptions, they are appended to the
    content before embedding so image context becomes searchable via RAG.
    """
    # Augment content with Vision descriptions (Priority 2: Vision-RAG)
    if ticket_id:
        try:
            topic = await db.get_topic(ticket_id)
            photos = (topic.photo_descriptions or "").strip() if topic else ""
            if photos:
                content = f"{content}\n\nОписания прикреплённых фото:\n{photos}"
        except Exception as exc:
            logger.warning("photo_descriptions fetch failed for %s: %s", ticket_id, exc)

    # Upsert first to get/create the DB row
    try:
        item_id, _ = await db.upsert_knowledge_item(
            source=source,
            content=content,
            ticket_id=ticket_id,
            title=title,
            quality=quality,
            url=url,
            content_hash=content_hash,
            company_id=company_id,
            company_name=company_name,
        )
    except Exception as exc:
        logger.warning("upsert_knowledge_item failed: %s", exc)
        return None
    # Embed cleaned version (raw content stays in DB for operator readability)
    embedding = await embed_text(clean_for_embedding(content), task_type="passage")
    if embedding is not None:
        try:
            emb_bytes = embedding_to_bytes(embedding)
            async with db.connect() as conn:
                await conn.execute(
                    "UPDATE knowledge_items SET embedding=? WHERE id=?",
                    (emb_bytes, item_id),
                )
                await conn.commit()
            from .store import invalidate_embeddings_cache  # noqa: PLC0415
            invalidate_embeddings_cache()
        except Exception as exc:
            logger.warning("Embedding update failed for item %s: %s", item_id, exc)
    logger.info("Indexed knowledge item id=%d source=%s", item_id, source)
    return item_id


# Порог cosine-схожести для включения примера в промпт (multilingual-e5-large).
# Замер на проде (scripts/_measure_rag_scores.py, 30 тикетов vs 1120 items,
# июнь 2026): релевантные top-1 0.88–0.96, нерелевантные запросы ≤0.79.
RAG_MIN_SCORE = 0.83


async def get_rag_context(
    ticket_title: str,
    history_tail: str,
    *,
    limit: int = 3,
    company_id: str = "",
) -> tuple[list[str], int]:
    """Return (content_list, max_confidence_pct) for top-N similar knowledge items.

    Items scoring below RAG_MIN_SCORE are dropped; with no confident match
    returns ([], 0) so the RAG block is skipped entirely.
    """
    query = f"{ticket_title}\n{history_tail[-600:]}"
    embedding = await embed_text(clean_for_embedding(query), task_type="query")
    if embedding is None:
        return [], 0
    similar = await find_similar(
        embedding, limit=limit, query_text=query, company_id=company_id,
        exclude_sources={KB_SOURCE},
    )
    confident = [(item, score) for item, score in similar if score >= RAG_MIN_SCORE]
    if not confident:
        return [], 0
    examples = [item.content for item, _ in confident]
    max_score = max(score for _, score in confident)
    confidence_pct = int(max_score * 100)
    return examples, confidence_pct


# --- Канал внутренней БЗ (выгрузка Teamly) ---
# Отдельный от примеров тикетов: у статей своя ниша в промпте («справочная
# статья»), свой порог и свой слот, поэтому чанки БЗ не сжигают слоты, в
# которых должны лежать похожие закрытые тикеты.
KB_SOURCE = "teamly"

# Порог и лексический якорь подобраны замером (scripts/_measure_kb_gate.py,
# август 2026). Абсолютного порога мало: настоящие top-1 лежат в 0.839–0.882,
# и в тот же диапазон попадают явно неверные попадания — внутренняя БЗ не
# покрывает железо, она про регламенты и админку. Поэтому статья показывается
# только если её ещё и находит BM25: хотя бы одно значимое слово тикета обязано
# встречаться в чанке.
KB_MIN_SCORE = 0.85
KB_POOL = 10
KB_MAX_CHARS = 2000


def kb_channel_enabled() -> bool:
    """Выключено по умолчанию: замер дал ~25% точности на выгрузке апреля 2026.

    Причина не в пороге, а в покрытии: выгрузка — общая БЗ первой линии
    (регламенты смен, админка, аналитика), а бот обслуживает отдел оборудования.
    Статей про Атол/PAX/принтеры в ней нет, поэтому гейт либо молчит, либо
    подставляет соседнюю тему. Включать, когда в source='teamly' появится
    контент по железу: KB_TEAMLY_ENABLED=1.
    """
    import os  # noqa: PLC0415

    return os.getenv("KB_TEAMLY_ENABLED", "").strip().lower() in {"1", "true", "yes"}


async def get_kb_context(ticket_title: str, history_tail: str) -> tuple[str, str] | None:
    """Вернуть (текст статьи, url) из внутренней БЗ или None, если не покрыто.

    Молчать — нормальный режим: дамп покрывает меньшую часть потока тикетов, а
    ложная статья хуже отсутствующей.
    """
    if not kb_channel_enabled():
        return None

    from .. import db as _db  # noqa: PLC0415

    query = f"{ticket_title}\n{history_tail[-600:]}"
    embedding = await embed_text(clean_for_embedding(query), task_type="query")
    if embedding is None:
        return None

    candidates = await find_similar(embedding, limit=KB_POOL, sources={KB_SOURCE})
    candidates = [(i, s) for i, s in candidates if s >= KB_MIN_SCORE]
    if not candidates:
        return None

    try:
        anchored = set(
            await _db.fts_search_source_any_token(query, KB_SOURCE, limit=KB_POOL)
        )
    except Exception as exc:
        logger.warning("KB lexical anchor lookup failed: %s", exc)
        return None
    if not anchored:
        return None

    for item, _score in candidates:
        if item.id in anchored:
            content = item.content
            if len(content) > KB_MAX_CHARS:
                content = content[:KB_MAX_CHARS] + "\n\n[...статья сокращена]"
            return content, (item.url or "")
    return None
