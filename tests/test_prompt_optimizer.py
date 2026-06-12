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

from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_llm_router_complete_all_returns_responses():
    """LLMRouter.complete_all returns dict model→response, skips unavailable models."""
    from bot.optimizer.llm_router import LLMRouter

    async def fake_complete(system, user):
        return "fake response"

    router = LLMRouter.__new__(LLMRouter)
    router.clients = {"llama": AsyncMock(complete=fake_complete)}

    results, errors = await router.complete_all(system="system", user="user")
    assert results == {"llama": "fake response"}
    assert errors == {}


class _FakeClient:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.called = False

    async def complete(self, system, user):
        self.called = True
        if self.error:
            raise RuntimeError(self.error)
        return self.text


def test_router_has_no_gemini_and_no_dead_gemma2():
    """All mutation clients are Groq; gemma2-9b-it is decommissioned on Groq."""
    from bot.optimizer.llm_router import LLMRouter
    router = LLMRouter(groq_api_key="k")
    assert "gemini" not in router.clients
    models = [getattr(c, "model", "") for c in router.clients.values()]
    assert "gemma2-9b-it" not in models


@pytest.mark.asyncio
async def test_complete_all_runs_groq_clients_in_parallel():
    """All Groq mutators contribute; OpenRouter stays untouched on success."""
    from bot.optimizer.llm_router import LLMRouter
    router = LLMRouter.__new__(LLMRouter)
    llama = _FakeClient(text="мутация от llama")
    gptoss = _FakeClient(text="мутация от gpt-oss")
    gemma4 = _FakeClient(text="мутация от gemma4")
    router.clients = {"llama": llama, "gptoss120": gptoss, "gemma4": gemma4}

    results, errors = await router.complete_all("s", "u")
    assert results == {
        "llama": "мутация от llama",
        "gptoss120": "мутация от gpt-oss",
    }
    assert gemma4.called is False


@pytest.mark.asyncio
async def test_complete_all_falls_back_to_openrouter_when_groq_fails():
    from bot.optimizer.llm_router import LLMRouter
    router = LLMRouter.__new__(LLMRouter)
    llama = _FakeClient(error="quota")
    gemma4 = _FakeClient(text="мутация от gemma4")
    router.clients = {"llama": llama, "gemma4": gemma4}

    results, errors = await router.complete_all("s", "u")
    assert results == {"gemma4": "мутация от gemma4"}
    assert "llama" in errors


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

    ref = "Клиенту: проверьте подключение принтера по USB и перезапустите кассу."
    samples = [
        {"history": "История", "title": "Тест", "ai_answer": "Ответ AI",
         "op_answer": ref, "outcome": "accepted", "confidence": 80},
    ]

    async def fake_generate(history, title, fmt):
        return ref  # perfect match with "Клиенту:" section

    score = await combined_score(samples, "инструкция", _generate_fn=fake_generate)
    assert score > 0.7


@pytest.mark.asyncio
async def test_combined_score_zero_when_no_client_section():
    """Structural check: no 'Клиенту:' section → score = 0 (prevents lazy outputs)."""
    from bot.optimizer.evaluator import combined_score

    ref = "Клиенту: проверьте подключение принтера по USB и перезапустите кассу."
    samples = [
        {"history": "История", "title": "Тест", "ai_answer": "AI",
         "op_answer": ref, "outcome": "sent", "confidence": 90},
    ]

    async def fake_generate(history, title, fmt):
        return ref.replace("Клиенту:", "").strip()  # same words but no structural marker

    score = await combined_score(samples, "инструкция", _generate_fn=fake_generate)
    assert score == 0.0


@pytest.mark.asyncio
async def test_combined_score_length_gate_penalizes_short():
    """Length gate: generated <30% of reference length → penalty 0.5."""
    from bot.optimizer.evaluator import combined_score

    ref = "Клиенту: " + ("проверьте подключение принтера и перезапустите кассу " * 10)
    samples = [
        {"history": "История", "title": "Тест", "ai_answer": "AI",
         "op_answer": ref, "outcome": "sent", "confidence": 90},
    ]

    async def fake_generate_short(history, title, fmt):
        return "Клиенту: ок"

    async def fake_generate_full(history, title, fmt):
        return ref

    short_score = await combined_score(samples, "инструкция", _generate_fn=fake_generate_short)
    full_score = await combined_score(samples, "инструкция", _generate_fn=fake_generate_full)
    assert short_score < full_score


@pytest.mark.asyncio
async def test_combined_score_jaccard_gate_penalizes_offtopic():
    """Jaccard gate: low word overlap with reference → penalty 0.7."""
    from bot.optimizer.evaluator import combined_score

    ref = "Клиенту: проверьте подключение принтера USB и перезапустите кассу атол"
    samples = [
        {"history": "История", "title": "Тест", "ai_answer": "AI",
         "op_answer": ref, "outcome": "sent", "confidence": 90},
    ]

    async def fake_offtopic(history, title, fmt):
        return "Клиенту: обратитесь поставщику бумаги магазина праздника салюта петарды"

    async def fake_ontopic(history, title, fmt):
        return ref

    off = await combined_score(samples, "инструкция", _generate_fn=fake_offtopic)
    on = await combined_score(samples, "инструкция", _generate_fn=fake_ontopic)
    assert off < on


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


# --- agent ---

@pytest.mark.asyncio
async def test_run_optimizer_skips_when_too_few_samples():
    """< 10 samples → optimizer returns early without sending report."""
    await _db.init_db()
    await _db.save_optimization_sample("T1", "Тест", "История", "AI ответ", "accepted")
    await _db.save_optimization_sample("T2", "Тест2", "История2", "AI ответ2", "rejected")

    from unittest.mock import AsyncMock, MagicMock, patch
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()

    with patch("bot.optimizer.agent.db", _db):
        with patch("bot.optimizer.agent._send_report", new=AsyncMock()) as mock_report:
            from bot.optimizer.agent import run_optimizer
            await run_optimizer(bot_mock)

    mock_report.assert_not_called()
    # Progress bar is still sent (UX feedback), but it must indicate the skip.
    progress_msg = bot_mock.send_message.return_value
    last_edit = progress_msg.edit_text.call_args
    assert last_edit is not None
    assert "ропущ" in last_edit.args[0] or "ропущ" in last_edit.kwargs.get("text", "")


# --- callbacks ---

@pytest.mark.asyncio
async def test_opt_apply_activates_version():
    """callback opt:apply:{id} → version becomes active."""
    await _db.init_db()
    vid = await _db.save_prompt_version("Новый промпт", 0.82, "llama")

    from unittest.mock import AsyncMock, MagicMock, patch
    callback = MagicMock()
    callback.data = f"opt:apply:{vid}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    with patch("bot.handlers.commands.db", _db):
        with patch("bot.handlers.commands.invalidate_prompt_cache") as mock_inv:
            from bot.handlers.commands import cb_opt_apply
            await cb_opt_apply(callback)

    active = await _db.get_active_prompt()
    assert active == "Новый промпт"
    mock_inv.assert_called_once()


# --- lazy prompt loading ---

@pytest.mark.asyncio
async def test_get_active_format_instructions_returns_builtin_when_no_db():
    """If no active version in DB — returns built-in _FORMAT_INSTRUCTIONS."""
    await _db.init_db()
    import bot.ai_summary as _ai_mod
    from bot.ai_summary import get_active_format_instructions, _FORMAT_INSTRUCTIONS
    # Reset cache
    _ai_mod._active_prompt_loaded = False
    with patch("bot.ai_summary.db", _db):
        result = await get_active_format_instructions()
    assert result == _FORMAT_INSTRUCTIONS


@pytest.mark.asyncio
async def test_get_active_format_instructions_returns_db_version():
    """If active version in DB — returns it."""
    await _db.init_db()
    vid = await _db.save_prompt_version("Кастомная инструкция", 0.85, "llama")
    await _db.apply_prompt_version(vid)

    import bot.ai_summary as _ai_mod
    _ai_mod._active_prompt_loaded = False  # reset cache

    with patch("bot.ai_summary.db", _db):
        from bot.ai_summary import get_active_format_instructions
        result = await get_active_format_instructions()
    assert result == "Кастомная инструкция"


@pytest.mark.asyncio
async def test_run_optimizer_applies_by_holdout_score(monkeypatch):
    """Победитель и apply-гейт определяются holdout_score, а не train-скором."""
    from bot.optimizer import agent

    samples = [
        {"id": i, "ticket_id": str(i), "title": "t", "history": f"h{i}",
         "ai_answer": "a", "op_answer": "Клиенту: ответ", "outcome": "corrected"}
        for i in range(20)
    ]

    async def fake_get_samples(days=30):
        return samples

    async def fake_combined(s, instructions, **kw):
        return 0.5  # train-скор одинаковый у всех

    holdout_calls = []

    async def fake_holdout(s, instructions, **kw):
        holdout_calls.append(instructions)
        return 0.4 if instructions == "CURRENT" else 0.9

    async def fake_get_active():
        return "CURRENT"

    class FakeRouter:
        def __init__(self, *a, **kw): pass
        async def complete_all(self, system, user):
            return {"gemini": "MUTATED INSTRUCTIONS LONG ENOUGH TO PASS"}, {}

    async def fake_save_version(content, score, proposed_by):
        return 7

    monkeypatch.setattr(agent.db, "get_optimization_samples", fake_get_samples)
    monkeypatch.setattr(agent.db, "save_prompt_version", fake_save_version)
    monkeypatch.setattr(agent, "combined_score", fake_combined)
    monkeypatch.setattr(agent, "holdout_score", fake_holdout)
    monkeypatch.setattr(agent, "get_active_format_instructions", fake_get_active)
    monkeypatch.setattr(agent, "LLMRouter", FakeRouter)

    sent = []

    class FakeMsg:
        async def edit_text(self, *a, **k):
            pass

    class FakeBot:
        async def send_message(self, **kw):
            sent.append(kw)
            return FakeMsg()

    await agent.run_optimizer(FakeBot())
    # holdout_score вызван и для CURRENT (baseline), и для мутации
    assert "CURRENT" in holdout_calls
    assert any("MUTATED" in c for c in holdout_calls)
    # отчёт с кнопкой apply отправлен (0.9 > 0.4 + 0.03)
    assert any("opt:apply" in str(kw.get("reply_markup", "")) for kw in sent)
