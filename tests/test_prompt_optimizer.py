import pytest
import aiosqlite
from bot import db as _db


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


# --- optimization_samples ---

@pytest.mark.asyncio
async def test_save_and_get_optimization_samples():
    await _db.init_db()
    await _db.save_optimization_sample(
        ticket_id="T1",
        title="Тест",
        history="История тикета",
        ai_answer="AI ответ",
        op_answer="Ответ оператора",
        outcome="accepted",
        confidence=75,
    )
    samples = await _db.get_optimization_samples(days=30)
    assert len(samples) == 1
    assert samples[0]["ticket_id"] == "T1"
    assert samples[0]["outcome"] == "accepted"
    assert samples[0]["ai_answer"] == "AI ответ"


@pytest.mark.asyncio
async def test_get_optimization_samples_filters_old():
    await _db.init_db()
    from datetime import datetime, timezone, timedelta
    old_date = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO optimization_samples (ticket_id, title, history, ai_answer, outcome, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("OLD", "старый", "история", "ответ", "accepted", old_date),
        )
        await db.commit()
    samples = await _db.get_optimization_samples(days=30)
    assert all(s["ticket_id"] != "OLD" for s in samples)


# --- prompt_versions ---

@pytest.mark.asyncio
async def test_get_active_prompt_returns_none_when_empty():
    await _db.init_db()
    result = await _db.get_active_prompt()
    assert result is None


@pytest.mark.asyncio
async def test_save_and_apply_prompt_version():
    await _db.init_db()
    version_id = await _db.save_prompt_version(
        content="Новая инструкция",
        score=0.82,
        proposed_by="llama",
    )
    assert version_id > 0

    await _db.apply_prompt_version(version_id)
    active = await _db.get_active_prompt()
    assert active == "Новая инструкция"


@pytest.mark.asyncio
async def test_apply_sets_others_rejected():
    await _db.init_db()
    id1 = await _db.save_prompt_version("Вариант A", 0.75, "gemini")
    id2 = await _db.save_prompt_version("Вариант B", 0.80, "llama")

    await _db.apply_prompt_version(id2)

    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT status FROM prompt_versions WHERE id=?", (id1,)
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == "rejected"


@pytest.mark.asyncio
async def test_reject_all_candidates():
    await _db.init_db()
    await _db.save_prompt_version("Кандидат", 0.70, "mixtral")
    await _db.reject_all_prompt_candidates()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM prompt_versions WHERE status='candidate'"
        ) as cur:
            count = (await cur.fetchone())[0]
    assert count == 0


# --- LLM Router ---

from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_llm_router_complete_all_returns_responses():
    """LLMRouter.complete_all returns dict model→response, skips unavailable models."""
    from bot.optimizer.llm_router import LLMRouter

    async def fake_complete(system, user):
        return "fake response"

    router = LLMRouter.__new__(LLMRouter)
    router.clients = {"gemini": AsyncMock(complete=fake_complete)}

    results = await router.complete_all(system="system", user="user")
    assert results == {"gemini": "fake response"}


@pytest.mark.asyncio
async def test_groq_client_skipped_when_no_api_key():
    """GroqClient.complete raises ValueError if no api key."""
    from bot.optimizer.llm_router import GroqClient
    client = GroqClient(model="llama-3.3-70b-versatile", api_key="")
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        await client.complete("system", "user")
