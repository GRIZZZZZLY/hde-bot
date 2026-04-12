# tests/test_ai_answer_quality.py
import asyncio
import pytest
import aiosqlite
from bot import db as _db

@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))

@pytest.mark.asyncio
async def test_save_and_find_pattern():
    await _db.init_db()
    pid = await _db.save_solution_pattern(
        problem_type="ошибка ОФД",
        steps="1. Меню ФН → 2. Диагностика ОФД",
        source="analyze",
        equipment="АТОЛ",
    )
    assert pid > 0
    result = await _db.find_solution_pattern("АТОЛ", "ошибка ОФД не отвечает")
    assert result is not None
    assert result["equipment"] == "АТОЛ"
    assert "ОФД" in result["steps"]

@pytest.mark.asyncio
async def test_increment_pattern_use():
    await _db.init_db()
    pid = await _db.save_solution_pattern("замена ФН", "Закрыть смену → Заменить ФН", "analyze")
    await _db.increment_pattern_use(pid)
    await _db.increment_pattern_use(pid)
    patterns = await _db.list_solution_patterns()
    assert patterns[0]["use_count"] == 2

@pytest.mark.asyncio
async def test_pattern_exists_similar_detects_duplicate():
    await _db.init_db()
    await _db.save_solution_pattern("ошибка соединения ОФД", "Шаг 1", "analyze", "АТОЛ")
    assert await _db.pattern_exists_similar("АТОЛ", "ошибка подключения ОФД") is True
    assert await _db.pattern_exists_similar("АТОЛ", "замена ФН регистратора") is False

@pytest.mark.asyncio
async def test_count_patterns_by_equipment():
    await _db.init_db()
    await _db.save_solution_pattern("ошибка ОФД", "Шаги", "analyze", "АТОЛ")
    await _db.save_solution_pattern("не печатает", "Шаги", "analyze", "Эвотор")
    await _db.save_solution_pattern("сеть", "Шаги", "analyze", None)
    counts = await _db.count_solution_patterns_by_equipment()
    assert counts.get("АТОЛ") == 1
    assert counts.get("Эвотор") == 1
    assert counts.get("Без бренда") == 1


# ---- Task 2 tests ----
from bot.ai_summary import _detect_equipment, _build_system_prompt

def test_detect_equipment_atol():
    assert _detect_equipment("АТОЛ 30Ф ошибка ОФД", "") == "АТОЛ"

def test_detect_equipment_in_history():
    assert _detect_equipment("Проблема с кассой", "У нас стоит Эвотор 7.2") == "Эвотор"

def test_detect_equipment_none():
    assert _detect_equipment("Не могу войти в личный кабинет", "нет связи") is None

def test_detect_equipment_acquiring():
    assert _detect_equipment("Терминал Сбера не работает", "") == "Эквайринг Сбер"

def test_build_system_prompt_contains_role():
    text = _build_system_prompt("тест")
    assert "2-й линии" in text
    assert "СПЕЦИАЛИСТУ" in text

def test_build_system_prompt_with_equipment():
    text = _build_system_prompt("тест", equipment="АТОЛ")
    assert "АТОЛ" in text

def test_build_system_prompt_with_steps():
    text = _build_system_prompt("тест", solution_steps="1. Меню ФН → 2. Диагностика")
    assert "Типовые шаги" in text
    assert "Меню ФН" in text


# ---- Task 3 tests ----
import numpy as np
from bot.knowledge.store import find_similar

@pytest.mark.asyncio
async def test_find_similar_returns_tuples():
    await _db.init_db()
    # Insert a knowledge item with embedding
    emb = np.ones(4, dtype=np.float32)
    emb_bytes = emb.tobytes()
    await _db.save_knowledge_item(
        source="test", content="АТОЛ ошибка ОФД",
        embedding=emb_bytes, quality="good", company_id="corp1",
    )
    results = await find_similar(emb, limit=1, query_text="")
    assert len(results) == 1
    item, score = results[0]
    assert 0.0 <= score <= 1.0
    assert "АТОЛ" in item.content

@pytest.mark.asyncio
async def test_find_similar_company_boost():
    await _db.init_db()
    # same-company item is slightly less similar but should rank first due to boost
    query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    same_co = np.array([0.9, 0.436, 0.0, 0.0], dtype=np.float32)  # ~cos 0.9
    diff_co = np.array([0.95, 0.312, 0.0, 0.0], dtype=np.float32) # ~cos 0.95
    same_co = same_co / np.linalg.norm(same_co)
    diff_co = diff_co / np.linalg.norm(diff_co)
    await _db.save_knowledge_item(
        source="test", content="same company item",
        embedding=same_co.tobytes(), quality="good", company_id="corp1",
    )
    await _db.save_knowledge_item(
        source="test", content="diff company item",
        embedding=diff_co.tobytes(), quality="good", company_id="corp2",
    )
    # Without boost: diff_co should rank higher (closer to query)
    results_no_boost = await find_similar(query, limit=2, company_id="")
    # With boost: same_co should rank first
    results_with_boost = await find_similar(query, limit=2, company_id="corp1")
    assert results_with_boost[0][0].content == "same company item"
