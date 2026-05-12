import re
import pytest


def strip_reasoning(text: str) -> str:
    """Strip <reasoning>...</reasoning> block from AI response."""
    return re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL).strip()


def test_strip_reasoning_removes_block():
    raw = (
        "<reasoning>\n"
        "1. Эвотор не видит ККТ.\n"
        "2. Атол 30Ф.\n"
        "3. Переподключение.\n"
        "</reasoning>\n\n"
        "Суть: Эвотор 5 не определяет Атол 30Ф по USB.\n"
        "Клиенту: Перезагрузите оба устройства.\n"
        "Памятка: Атол 30Ф + Эвотор 5 • USB."
    )
    result = strip_reasoning(raw)
    assert "<reasoning>" not in result
    assert "Суть:" in result
    assert "Клиенту:" in result


def test_strip_reasoning_noop_when_absent():
    raw = "Суть: Проблема X.\nКлиенту: Сделайте Y.\nПамятка: —"
    assert strip_reasoning(raw) == raw


def test_strip_reasoning_multiline():
    raw = "<reasoning>\nline1\nline2\n</reasoning>\nСуть: X.\nКлиенту: Y.\nПамятка: —"
    result = strip_reasoning(raw)
    assert result.startswith("Суть:")


def test_few_shot_injection_in_prompt(monkeypatch):
    import json
    import bot.ai_summary as ai_mod

    monkeypatch.setattr(ai_mod, "_FEW_SHOT_EXAMPLES", [
        {
            "problem": "Тест проблема",
            "suit": "Тест суть.",
            "client": "Тест клиенту.",
            "pamyatka": "Тест памятка."
        }
    ])

    prompt = ai_mod._build_system_prompt("Тест тикет")
    assert "Эталонные примеры" in prompt
    assert "Тест суть." in prompt
    assert "Тест клиенту." in prompt
