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
