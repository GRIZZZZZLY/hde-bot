"""Развязка источников в find_similar.

Чанки внутренней БЗ не должны конкурировать с примерами закрытых тикетов за
одни и те же слоты в промпте: статья идёт отдельным блоком, тикеты — своим.
"""
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from bot.knowledge.store import embedding_to_bytes


def _rows():
    """Три строки: два тикета и один чанк БЗ, вектора попарно различимы."""
    ticket_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    kb_vec = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    return [
        (1, "закрытый тикет про Атол", embedding_to_bytes(ticket_vec), "", "hde_closed"),
        (2, "фидбэк оператора", embedding_to_bytes(ticket_vec), "", "feedback"),
        (3, "регламент из внутренней БЗ", embedding_to_bytes(kb_vec), "", "teamly"),
    ]


async def _find(query, **kw):
    from bot.knowledge import store as store_mod

    store_mod.invalidate_embeddings_cache()
    with patch.object(
        store_mod, "list_all_knowledge_embeddings", AsyncMock(return_value=_rows())
    ):
        return await store_mod.find_similar(query, limit=3, **kw)


@pytest.mark.asyncio
async def test_sources_filter_returns_only_requested_source():
    """sources={'teamly'} отдаёт только чанк БЗ, даже если тикеты похожи сильнее."""
    query = np.array([1.0, 0.2, 0.0], dtype=np.float32)  # ближе к тикетам
    result = await _find(query, sources={"teamly"})
    assert [item.id for item, _ in result] == [3]


@pytest.mark.asyncio
async def test_exclude_sources_drops_kb_chunks():
    query = np.array([0.0, 1.0, 0.0], dtype=np.float32)  # ближе к БЗ
    result = await _find(query, exclude_sources={"teamly"})
    assert {item.id for item, _ in result} == {1, 2}


@pytest.mark.asyncio
async def test_no_filter_returns_everything():
    query = np.array([1.0, 1.0, 0.0], dtype=np.float32)
    result = await _find(query)
    assert {item.id for item, _ in result} == {1, 2, 3}


@pytest.mark.asyncio
async def test_unknown_source_returns_empty_not_garbage():
    """Пустой источник не должен просачивать чужие строки через пул."""
    query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert await _find(query, sources={"nonexistent"}) == []


@pytest.mark.asyncio
async def test_filtered_scores_are_real_cosines():
    """Скор отфильтрованного результата — косинус, а не служебная -1.0."""
    query = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    result = await _find(query, sources={"teamly"})
    assert result[0][1] == pytest.approx(1.0, abs=1e-5)


# --- AND-гейт канала БЗ: косинус И лексический якорь ---

def _item(item_id, content, url):
    from bot.db import KnowledgeItem

    return KnowledgeItem(
        id=item_id, source="teamly", ticket_id=None, title="Статья",
        content=content, quality="good", url=url,
    )


async def _kb(candidates, anchored):
    from bot.knowledge import indexer

    with patch.object(indexer, "embed_text",
                      AsyncMock(return_value=np.ones(4, dtype=np.float32))),          patch.object(indexer, "find_similar", AsyncMock(return_value=candidates)),          patch("bot.db.fts_search_source_any_token", AsyncMock(return_value=anchored)),          patch.object(indexer, "kb_channel_enabled", lambda: True):
        return await indexer.get_kb_context("Атол нет связи", "касса не отвечает")


@pytest.mark.asyncio
async def test_kb_returns_article_when_cosine_and_anchor_agree():
    from bot.knowledge.indexer import KB_MIN_SCORE

    got = await _kb([(_item(7, "Проверьте COM-порт", "https://t.ru/at/x"), KB_MIN_SCORE)], [7])
    assert got == ("Проверьте COM-порт", "https://t.ru/at/x")


@pytest.mark.asyncio
async def test_kb_silent_when_lexical_anchor_missing():
    """Высокий косинус без общих слов — это и есть ложное срабатывание."""
    got = await _kb([(_item(7, "Регламент звонков", "https://t.ru/at/x"), 0.95)], [99])
    assert got is None


@pytest.mark.asyncio
async def test_kb_silent_below_threshold():
    from bot.knowledge.indexer import KB_MIN_SCORE

    got = await _kb([(_item(7, "текст", "u"), KB_MIN_SCORE - 0.01)], [7])
    assert got is None


@pytest.mark.asyncio
async def test_kb_silent_when_nothing_anchored():
    got = await _kb([(_item(7, "текст", "u"), 0.99)], [])
    assert got is None


@pytest.mark.asyncio
async def test_kb_skips_unanchored_and_takes_next_candidate():
    """Первый кандидат без якоря не должен блокировать второго."""
    cands = [
        (_item(1, "мимо", "u1"), 0.95),
        (_item(2, "в точку", "u2"), 0.90),
    ]
    assert await _kb(cands, [2]) == ("в точку", "u2")


@pytest.mark.asyncio
async def test_kb_truncates_long_article():
    from bot.knowledge.indexer import KB_MAX_CHARS

    long_text = "а" * (KB_MAX_CHARS + 500)
    content, _ = await _kb([(_item(7, long_text, "u"), 0.95)], [7])
    assert len(content) < len(long_text)
    assert content.endswith("[...статья сокращена]")


@pytest.mark.asyncio
async def test_kb_channel_disabled_by_default():
    """Без KB_TEAMLY_ENABLED канал молчит: замер дал ~25% точности."""
    import os

    from bot.knowledge import indexer

    with patch.dict(os.environ, {"KB_TEAMLY_ENABLED": ""}, clear=False):
        assert indexer.kb_channel_enabled() is False
        assert await indexer.get_kb_context("Атол нет связи", "не отвечает") is None


@pytest.mark.asyncio
async def test_kb_channel_enabled_by_env():
    import os

    from bot.knowledge import indexer

    with patch.dict(os.environ, {"KB_TEAMLY_ENABLED": "1"}, clear=False):
        assert indexer.kb_channel_enabled() is True
