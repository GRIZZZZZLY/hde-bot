"""Tests for Окружение autofill improvements: env_option_id storage,
company prior, keyword pre-pass, enriched classification context."""
import pytest

from bot import db as _db


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


# --- env_option_id column ---

@pytest.mark.asyncio
async def test_env_option_id_roundtrip():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    await _db.update_topic("t1", env_option_id="145")
    rec = await _db.get_topic("t1")
    assert rec.env_option_id == "145"


@pytest.mark.asyncio
async def test_env_option_id_default_none():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    rec = await _db.get_topic("t1")
    assert rec.env_option_id is None


# --- company prior ---

@pytest.mark.asyncio
async def test_get_common_env_for_company_most_frequent():
    await _db.init_db()
    for i, env in enumerate(["146", "146", "145"]):
        await _db.upsert_topic(f"t{i}", 100 + i)
        await _db.update_topic(f"t{i}", company_name="ООО Ромашка", env_option_id=env)
    await _db.upsert_topic("t9", 999)
    await _db.update_topic("t9", company_name="ООО Ромашка")
    assert await _db.get_common_env_for_company("ООО Ромашка", exclude_ticket_id="t9") == "146"


@pytest.mark.asyncio
async def test_get_common_env_for_company_excludes_current_and_empty():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    await _db.update_topic("t1", company_name="ООО Ромашка", env_option_id="")
    assert await _db.get_common_env_for_company("ООО Ромашка", exclude_ticket_id="t2") is None
    assert await _db.get_common_env_for_company("", exclude_ticket_id="t2") is None
