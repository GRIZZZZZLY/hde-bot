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


# --- data collection ---

@pytest.mark.asyncio
async def test_implicit_feedback_saves_accepted_sample():
    """ratio >= 0.7 → save record outcome='accepted' in optimization_samples."""
    await _db.init_db()

    # Save pending feedback record
    from datetime import datetime, timezone, timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await db.execute(
            "INSERT INTO ai_feedback_pending (topic_id, ticket_id, title, history, answer_text, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (1, "T1", "Тест", "История тикета", "Нажмите кнопку обновить", expires),
        )
        await db.commit()

    # Simulate staff reply with similar text
    from unittest.mock import AsyncMock, MagicMock, patch
    with patch("bot.topic_manager.db", _db):
        with patch("bot.knowledge.indexer.index_knowledge_item", new=AsyncMock()):
            with patch("bot.db.delete_knowledge_item_by_ticket", new=AsyncMock(return_value=0)):
                with patch("bot.topic_manager._maybe_update_pattern", new=AsyncMock()):
                    from bot.topic_manager import _implicit_feedback
                    record = MagicMock()
                    record.topic_id = 1
                    await _implicit_feedback(record, "Нажмите кнопку обновления")

    samples = await _db.get_optimization_samples(days=1)
    assert len(samples) == 1
    assert samples[0]["outcome"] == "accepted"
    assert samples[0]["ticket_id"] == "T1"


# --- mutations ---

def test_build_mutation_prompt_contains_current_instructions():
    from bot.optimizer.mutations import build_mutation_prompt
    system, user = build_mutation_prompt(
        current_instructions="Ответь двумя строками.",
        good_examples=[{"ai_answer": "Хороший ответ", "op_answer": "Хороший ответ"}],
        bad_examples=[{"ai_answer": "Плохой ответ", "op_answer": None}],
    )
    assert "Ответь двумя строками." in system
    assert "Хороший ответ" in user
    assert "Плохой ответ" in user


# --- evaluator ---

@pytest.mark.asyncio
async def test_combined_score_perfect_acceptance():
    """If all generated answers match op_answer — score near 1."""
    from bot.optimizer.evaluator import combined_score

    samples = [
        {"history": "История", "title": "Тест", "ai_answer": "Ответ AI",
         "op_answer": "Ответ оператора", "outcome": "accepted", "confidence": 80},
    ]

    async def fake_generate(history, title, fmt):
        return "Ответ оператора"  # perfect match

    score = await combined_score(samples, "инструкция", _generate_fn=fake_generate)
    assert score > 0.7


@pytest.mark.asyncio
async def test_combined_score_all_rejected():
    """All rejected — score = 0."""
    from bot.optimizer.evaluator import combined_score

    samples = [
        {"history": "История", "title": "Тест", "ai_answer": "Ответ AI",
         "op_answer": None, "outcome": "rejected", "confidence": 30},
    ]

    async def fake_generate(history, title, fmt):
        return "что-то"

    score = await combined_score(samples, "инструкция", _generate_fn=fake_generate)
    assert score == 0.0


@pytest.mark.asyncio
async def test_combined_score_empty_samples():
    """Empty dataset — score = 0."""
    from bot.optimizer.evaluator import combined_score

    async def fake_generate(history, title, fmt):
        return "что-то"

    score = await combined_score([], "инструкция", _generate_fn=fake_generate)
    assert score == 0.0
