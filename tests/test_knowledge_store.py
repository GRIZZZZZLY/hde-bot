from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from bot.knowledge.store import cosine_similarity, embedding_to_bytes, bytes_to_embedding


def test_cosine_similarity_identical():
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert cosine_similarity(a, a) == pytest.approx(1.0, abs=1e-5)


def test_cosine_similarity_orthogonal():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-5)


def test_embedding_roundtrip():
    original = np.random.rand(768).astype(np.float32)
    restored = bytes_to_embedding(embedding_to_bytes(original))
    np.testing.assert_array_almost_equal(original, restored)


@pytest.mark.asyncio
async def test_embed_text_returns_ndarray():
    """embed_text returns a float32 ndarray of shape (EMBEDDING_DIM,)."""
    from unittest.mock import MagicMock, patch

    fake_embedding = np.ones(1024, dtype=np.float32)
    mock_model = MagicMock()
    mock_model.encode.return_value = fake_embedding

    with patch("bot.knowledge.indexer._load_model", return_value=mock_model), \
         patch("bot.knowledge.indexer._model", mock_model):
        from bot.knowledge.indexer import embed_text, EMBEDDING_DIM
        result = await embed_text("тестовый текст")

    assert result is not None
    assert result.shape == (EMBEDDING_DIM,)
    assert result.dtype == np.float32


# --- Priority 3: in-memory embedding cache ---


@pytest.mark.asyncio
async def test_cache_hit_skips_db_within_ttl():
    """Second call within TTL must not hit list_all_knowledge_embeddings."""
    from unittest.mock import AsyncMock, patch
    from bot.knowledge import store as store_mod

    emb = np.ones(1024, dtype=np.float32)
    raw = [(1, "content", embedding_to_bytes(emb), "comp1")]

    # Reset cache state
    store_mod.invalidate_embeddings_cache()

    with patch.object(store_mod, "list_all_knowledge_embeddings", AsyncMock(return_value=raw)) as mock_db:
        rows1 = await store_mod._load_embeddings_cached()
        rows2 = await store_mod._load_embeddings_cached()
        rows3 = await store_mod._load_embeddings_cached()

    assert mock_db.call_count == 1  # only first call hit DB
    assert len(rows1) == 1
    assert rows1 is rows2 is rows3  # same cached list instance


@pytest.mark.asyncio
async def test_invalidate_forces_reload():
    """invalidate_embeddings_cache forces next call to reload from DB."""
    from unittest.mock import AsyncMock, patch
    from bot.knowledge import store as store_mod

    emb = np.ones(1024, dtype=np.float32)
    raw = [(1, "content", embedding_to_bytes(emb), "comp1")]

    store_mod.invalidate_embeddings_cache()

    with patch.object(store_mod, "list_all_knowledge_embeddings", AsyncMock(return_value=raw)) as mock_db:
        await store_mod._load_embeddings_cached()
        store_mod.invalidate_embeddings_cache()
        await store_mod._load_embeddings_cached()

    assert mock_db.call_count == 2  # invalidation triggered reload


# --- Priority 4: clean_for_embedding ---


def test_clean_strips_signature_russian():
    from bot.knowledge.indexer import clean_for_embedding

    text = "Ошибка ФН 234 на кассе АТОЛ.\n\nС уважением,\nИван Петров\nООО Ромашка"
    cleaned = clean_for_embedding(text)
    assert "Ошибка ФН 234" in cleaned
    assert "Иван Петров" not in cleaned
    assert "Ромашка" not in cleaned


def test_clean_strips_signature_english():
    from bot.knowledge.indexer import clean_for_embedding

    text = "Printer offline.\n\nBest regards,\nJohn"
    cleaned = clean_for_embedding(text)
    assert "Printer offline" in cleaned
    assert "John" not in cleaned


def test_clean_strips_urls_emails_and_quoted():
    from bot.knowledge.indexer import clean_for_embedding

    text = (
        "Проблема с чеком. Ссылка https://example.com/ticket/123\n"
        "Контакт: ivan@company.ru\n"
        "> цитата предыдущего письма\n"
        "тикет #12345"
    )
    cleaned = clean_for_embedding(text)
    assert "https://" not in cleaned
    assert "@" not in cleaned
    assert "цитата предыдущего письма" not in cleaned
    assert "#12345" not in cleaned
    assert "Проблема с чеком" in cleaned


def test_clean_collapses_whitespace():
    from bot.knowledge.indexer import clean_for_embedding

    text = "Строка1\n\n\n\nСтрока2   с     пробелами"
    cleaned = clean_for_embedding(text)
    assert "\n\n\n" not in cleaned
    assert "   " not in cleaned


def test_clean_empty_returns_empty():
    from bot.knowledge.indexer import clean_for_embedding

    assert clean_for_embedding("") == ""
    assert clean_for_embedding(None) == ""  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cache_skips_corrupted_embeddings():
    """Corrupted bytes in one row don't break cache; row is skipped."""
    from unittest.mock import AsyncMock, patch
    from bot.knowledge import store as store_mod

    good = embedding_to_bytes(np.ones(1024, dtype=np.float32))
    bad = b"\x00\x01"  # wrong length → frombuffer raises
    raw = [(1, "good", good, ""), (2, "bad", bad, "")]

    store_mod.invalidate_embeddings_cache()

    with patch.object(store_mod, "list_all_knowledge_embeddings", AsyncMock(return_value=raw)):
        rows = await store_mod._load_embeddings_cached()

    ids = [r[0] for r in rows]
    assert 1 in ids
    assert 2 not in ids  # corrupted row dropped
