from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
    mock_response = {
        "embedding": {"values": [0.1] * 768}
    }
    with patch("bot.knowledge.indexer.config") as mock_config, \
         patch("bot.knowledge.indexer.aiohttp.ClientSession") as mock_session_cls:
        mock_config.gemini_api_key = "test-key"
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=mock_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_get = AsyncMock()
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_get)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        from bot.knowledge.indexer import embed_text
        result = await embed_text("тестовый текст")

    assert result is not None
    assert result.shape == (768,)
    assert result.dtype == np.float32
