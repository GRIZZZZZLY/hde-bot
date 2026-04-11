from __future__ import annotations

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
