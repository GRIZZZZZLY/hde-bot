import pytest
from bot.ai_summary import _strip_reasoning


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
    result = _strip_reasoning(raw)
    assert "<reasoning>" not in result
    assert "Суть:" in result
    assert "Клиенту:" in result


def test_strip_reasoning_noop_when_absent():
    raw = "Суть: Проблема X.\nКлиенту: Сделайте Y.\nПамятка: —"
    assert _strip_reasoning(raw) == raw


def test_strip_reasoning_multiline():
    raw = "<reasoning>\nline1\nline2\n</reasoning>\nСуть: X.\nКлиенту: Y.\nПамятка: —"
    result = _strip_reasoning(raw)
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


def test_few_shot_examples_are_not_dominated_by_remote_access():
    """Правило «сначала точное действие, потом удалёнка» проигрывало примерам:
    4 из 5 эталонов были про AnyDesk/RuDesktop, и модель предлагала удалёнку
    даже там, где оператор чинил порт или отправлял в банк (сверка 2026-09)."""
    from bot.ai_summary import _load_few_shot_examples

    examples = _load_few_shot_examples()
    assert len(examples) >= 5
    remote = [
        e for e in examples
        if any(w in e["client"].lower() for w in ("anydesk", "rudesktop", "удал"))
    ]
    assert len(remote) * 3 <= len(examples), (
        f"удалёнка в {len(remote)} из {len(examples)} эталонов «Клиенту» — "
        "примеры снова перевешивают правило"
    )


def test_remote_access_examples_carry_link_and_what_to_send():
    """Если пример всё-таки про удалёнку — он показывает канонический макрос
    оператора: ссылка на программу и что прислать в ответ. Иначе модель
    воспроизводит «пришлите ID», а оператор просит номер рабочего места."""
    from bot.ai_summary import _load_few_shot_examples

    for ex in _load_few_shot_examples():
        text = f"{ex['client']} {ex['pamyatka']}".lower()
        if "anydesk.com" in text or "rudesktop.ru" in text:
            assert "номер рабочего места" in text, ex["problem"]


def test_no_first_person_completed_actions_in_examples():
    """«Подключаюсь к вам» / «Подключился» в эталоне учит бота обещать
    несделанное — тикеты 194769 и 195723."""
    from bot.ai_summary import _load_few_shot_examples

    banned = ("подключаюсь", "подключился", "настроил вам", "обновил вам")
    for ex in _load_few_shot_examples():
        low = ex["client"].lower()
        assert not any(b in low for b in banned), ex["problem"]
