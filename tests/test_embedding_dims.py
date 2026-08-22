"""Вектор чужой размерности не должен убивать весь RAG.

Один item с эмбеддингом от другой модели ронял `matmul` в find_similar — то есть
поиск умирал на ВСЕХ тикетах, а не на этой записи. Здесь проверяется, что такая
строка просто исключается из матрицы.
"""
import numpy as np
import pytest

import bot.db as db_module
from bot.knowledge import store


@pytest.fixture(autouse=True)
def clean_matrix_cache():
    store.invalidate_embeddings_cache()
    store._matrix_rows = None
    store._matrix = None
    store._matrix_norms = None
    store._matrix_dim = None
    store._matrix_kept = None
    yield


async def _save(content: str, embedding: np.ndarray, source: str = "test") -> int:
    return await db_module.save_knowledge_item(
        source=source, content=content,
        embedding=embedding.astype(np.float32).tobytes(),
        quality="good", company_id="",
    )


@pytest.mark.asyncio
async def test_foreign_dimension_row_does_not_break_search():
    await db_module.init_db()
    await _save("правильная запись", np.ones(8) / 8)
    await _save("запись от другой модели", np.ones(4) / 4)   # чужая размерность
    store.invalidate_embeddings_cache()

    results = await store.find_similar(np.ones(8, dtype=np.float32) / 8, limit=5)

    contents = [item.content for item, _ in results]
    assert "правильная запись" in contents
    assert "запись от другой модели" not in contents


@pytest.mark.asyncio
async def test_all_rows_foreign_returns_empty_instead_of_raising():
    """Пустой результат — легальный исход (RAG-блок просто исчезает из промпта)."""
    await db_module.init_db()
    await _save("другая модель", np.ones(4) / 4)
    store.invalidate_embeddings_cache()

    assert await store.find_similar(np.ones(1024, dtype=np.float32) / 1024) == []


@pytest.mark.asyncio
async def test_matrix_rebuilds_when_query_dimension_changes():
    """Кеш матрицы держится за размерность: смена запроса не должна отдавать старую."""
    await db_module.init_db()
    await _save("восьмёрка", np.ones(8) / 8)
    await _save("четвёрка", np.ones(4) / 4)
    store.invalidate_embeddings_cache()

    eight = await store.find_similar(np.ones(8, dtype=np.float32) / 8, limit=5)
    four = await store.find_similar(np.ones(4, dtype=np.float32) / 4, limit=5)

    assert [i.content for i, _ in eight] == ["восьмёрка"]
    assert [i.content for i, _ in four] == ["четвёрка"]
