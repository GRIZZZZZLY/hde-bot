"""Промпт v2: бюджет, порядок блоков, сигналы (spec 2026-09-27 §4)."""
from bot.agent import voice


def _worst_context() -> dict:
    return {
        "ticket_facts": "Компания клиента: " + "Ц" * 300,
        "attachments": "А" * 900,
        "call_notes": "З" * 900,
        "evidence": [
            {"source_type": "knowledge_item", "source_id": 1, "used_excerpt": "К" * 900},
            {"source_type": "knowledge_item", "source_id": 2, "used_excerpt": "К" * 900},
        ],
        "demos": [
            {"source_type": "dialogue_pair", "source_id": 7, "used_excerpt": "П" * 500},
            {"source_type": "dialogue_pair", "source_id": 8, "used_excerpt": "П" * 500},
        ],
        "stress": True,
        "first_staff_reply": True,
    }


def test_worst_case_fits_budget_with_full_history():
    system = voice.build_prompt(_worst_context(), "Т" * 200)
    history_budget = 3000
    assert len(system) + history_budget <= 11_500, len(system)
    assert len(system) <= voice.SYSTEM_BUDGET_CHARS


def test_voice_comes_before_ticket_data_and_format_is_last():
    system = voice.build_prompt(_worst_context(), "Касса не печатает")
    assert system.index("ГОЛОС ОТВЕТА КЛИЕНТУ") < system.index("Источники")
    assert system.rstrip().endswith("}")          # формат JSON — последним
    assert '"analysis"' in system


def test_signals_are_rendered_only_when_set():
    calm = voice.build_prompt({"stress": False, "first_staff_reply": False}, "T")
    assert "Сигнал: стресс" not in calm
    assert "Первый ответ: нет" in calm
    tense = voice.build_prompt({"stress": True, "first_staff_reply": True}, "T")
    assert "Сигнал: стресс" in tense
    assert "Первый ответ: да" in tense


def test_sources_are_labelled_with_the_ids_lint_expects():
    system = voice.build_prompt(_worst_context(), "T")
    assert "[KB#1]" in system and "[пара#7]" in system


def test_old_style_rules_do_not_leak_in():
    system = voice.build_prompt({}, "T")
    assert "≤20 слов" not in system
    assert "номер рабочего места" in system        # регламент: как на экране AnyDesk
