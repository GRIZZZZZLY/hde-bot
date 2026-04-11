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
