"""Embed text via Gemini text-embedding-004 and index into knowledge store."""
from __future__ import annotations

import logging

import aiohttp
import numpy as np

from ..config import config
from .store import find_similar, save_and_index

logger = logging.getLogger(__name__)

_EMBEDDING_MODEL = "text-embedding-004"
_EMBEDDING_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_EMBEDDING_MODEL}:embedContent"
)


async def embed_text(text: str) -> np.ndarray | None:
    """Return 768-dim float32 embedding or None on failure."""
    if not config.gemini_api_key:
        return None
    payload = {
        "model": f"models/{_EMBEDDING_MODEL}",
        "content": {"parts": [{"text": text[:8000]}]},  # API limit
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _EMBEDDING_URL,
                json=payload,
                params={"key": config.gemini_api_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Embedding API error %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
    except Exception as exc:
        logger.warning("embed_text failed: %s", exc)
        return None

    values = data.get("embedding", {}).get("values", [])
    if not values:
        return None
    return np.array(values, dtype=np.float32)


async def index_knowledge_item(
    source: str,
    content: str,
    *,
    ticket_id: str = "",
    title: str = "",
    quality: str = "good",
    url: str = "",
) -> int | None:
    """Embed content and save to knowledge store. Returns item id or None."""
    embedding = await embed_text(content)
    if embedding is None:
        logger.warning("Could not embed knowledge item (source=%s), skipping", source)
        return None
    item_id = await save_and_index(
        source=source,
        content=content,
        embedding=embedding,
        ticket_id=ticket_id,
        title=title,
        quality=quality,
        url=url,
    )
    logger.info("Indexed knowledge item id=%d source=%s", item_id, source)
    return item_id


async def get_rag_context(
    ticket_title: str,
    history_tail: str,
    *,
    limit: int = 3,
) -> list[str]:
    """Return list of content strings for top-N similar knowledge items."""
    query = f"{ticket_title}\n{history_tail[-600:]}"
    embedding = await embed_text(query)
    if embedding is None:
        return []
    similar = await find_similar(embedding, limit=limit)
    return [item.content for item in similar]
