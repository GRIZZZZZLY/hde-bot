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
