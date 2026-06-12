"""Tests for Приоритет/Тип autofill: combo table, prompt, parser,
classifier, HDE client extension, DB columns, apply wiring, outcome log."""
import pytest

from bot import db as _db


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


# --- combo table & parser ---

def test_pt_combos_cover_six_rules():
    from bot.ticket_fields import _PT_COMBOS
    assert set(_PT_COMBOS) == {"1", "2", "3", "4", "5", "6"}
    # ускоренный+ошибка, стандарт+ошибка, ускоренный+задача,
    # стандарт+задача, низкий+вопрос, низкий+задача
    assert _PT_COMBOS["1"] == ("1", "3")
    assert _PT_COMBOS["2"] == ("10", "3")
    assert _PT_COMBOS["3"] == ("1", "2")
    assert _PT_COMBOS["4"] == ("10", "2")
    assert _PT_COMBOS["5"] == ("3", "0")
    assert _PT_COMBOS["6"] == ("3", "2")


def test_parse_pt_combo_plain_number():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("1") == ("1", "3")


def test_parse_pt_combo_with_reasoning_text():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("Касса не работает, торговля стоит. Ответ: 1") == ("1", "3")


def test_parse_pt_combo_undetermined():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("НЕ ОПРЕДЕЛЕНО") is None


def test_parse_pt_combo_out_of_range_rejected():
    from bot.ticket_fields import _parse_pt_combo
    # 7 и 10 — не номера комбинаций; "10" не должен распадаться на "1"
    assert _parse_pt_combo("7") is None
    assert _parse_pt_combo("10") is None


def test_parse_pt_combo_empty():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("") is None


def test_pt_prompt_contains_all_combos_and_undetermined():
    from bot.ticket_fields import _build_pt_prompt
    p = _build_pt_prompt()
    for num in ("1 =", "2 =", "3 =", "4 =", "5 =", "6 ="):
        assert num in p
    assert "НЕ ОПРЕДЕЛЕНО" in p
    assert "инженера банка" in p.lower() or "инженер банка" in p.lower()


# --- classify_priority_type ---

@pytest.mark.asyncio
async def test_classify_pt_returns_combo(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock(return_value="1")
    monkeypatch.setattr(tf, "_groq_classify", groq)
    result = await tf.classify_priority_type(
        "Клиент: касса не включается, продавать не можем",
        ticket_title="Касса не работает",
    )
    assert result == ("1", "3")
    prompt_arg, user_content = groq.await_args.args
    assert "НЕ ОПРЕДЕЛЕНО" in prompt_arg
    assert "Тема тикета: Касса не работает" in user_content
    assert "Переписка:\nКлиент: касса не включается" in user_content


@pytest.mark.asyncio
async def test_classify_pt_falls_back_to_scout(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock(side_effect=[None, "4"])
    monkeypatch.setattr(tf, "_groq_classify", groq)
    result = await tf.classify_priority_type("Клиент: настройте принтер")
    assert result == ("10", "2")
    assert groq.await_count == 2
    assert groq.await_args_list[1].kwargs["model"] == tf._GROQ_FALLBACK_MODEL


@pytest.mark.asyncio
async def test_classify_pt_undetermined(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    monkeypatch.setattr(tf, "_groq_classify", AsyncMock(return_value="НЕ ОПРЕДЕЛЕНО"))
    assert await tf.classify_priority_type("Клиент: привет") is None


@pytest.mark.asyncio
async def test_classify_pt_empty_input_skips_llm(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock()
    monkeypatch.setattr(tf, "_groq_classify", groq)
    assert await tf.classify_priority_type("", ticket_title="") is None
    groq.assert_not_awaited()
