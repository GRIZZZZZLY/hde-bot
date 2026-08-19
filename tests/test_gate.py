"""Apply-гейт на парных дельтах и hard-checks кандидата (спека §11.2, §11.7).

Главное, что здесь проверяется: кандидат с положительным средним, но широким
разбросом по кейсам, гейт НЕ проходит. Разница двух средних этого не видит — а
именно она и решала раньше (`_MIN_IMPROVEMENT = 0.03`).
"""
import pytest

from bot.optimizer.checks import (
    LENGTH_PENALTY,
    client_facing_text,
    hard_check,
    word_limit,
)
from bot.optimizer.gate import (
    apply_gate,
    axis_regressions,
    bootstrap_ci_low,
    paired_deltas,
)


class _Decision:
    def __init__(self, action="PROCEED", category=None):
        self.action = action
        self.category = category


def _safe(_text):
    return _Decision()


def _unsafe(_text):
    return _Decision("ESCALATE", "finance")


# --- hard-checks ------------------------------------------------------------

def test_client_facing_text_strips_reasoning_and_operator_sections():
    text = (
        "<reasoning>внутренние размышления</reasoning>\n"
        "Суть: касса не печатает\n"
        "Клиенту: перезагрузите кассу\n"
        "Памятка: вернуть деньги клиенту нельзя без оператора"
    )
    client = client_facing_text(text)
    assert client == "перезагрузите кассу"
    assert "размышления" not in client
    assert "вернуть деньги" not in client  # операторская секция не идёт в safety-скан


def test_no_client_section_is_fatal():
    result = hard_check("Суть: что-то\nПамятка: —", _safety_fn=_safe)
    assert result.fatal == "no_client_section"


def test_empty_answer_is_fatal():
    assert hard_check("   ", _safety_fn=_safe).fatal == "empty"


def test_reasoning_leak_into_client_text_is_fatal():
    leaked = "Клиенту: <think>подумаю</think> перезагрузите кассу"
    assert hard_check(leaked, _safety_fn=_safe).fatal == "reasoning_leak"


def test_safety_category_in_client_text_is_fatal():
    result = hard_check("Клиенту: сделаем возврат средств", _safety_fn=_unsafe)
    assert result.fatal == "safety"
    assert "finance" in result.detail


def test_operator_section_safety_does_not_trigger():
    """Фискальное действие в Памятке — диагноз оператору, не инструкция клиенту."""
    text = "Клиенту: пришлите фото экрана\nПамятка: клиент просит вернуть деньги"

    seen = []

    def spy(client_text):
        seen.append(client_text)
        return _Decision()

    hard_check(text, _safety_fn=spy)
    assert seen == ["пришлите фото экрана"]


def test_length_penalty_depends_on_action_type():
    """ASK обязан быть короче ANSWER — один лимит на всё неверен."""
    assert word_limit("ASK") < word_limit("ANSWER")
    client = "Клиенту: " + " ".join(f"слово{i}" for i in range(20))
    as_ask = hard_check(client, action_type="ASK", _safety_fn=_safe)
    as_answer = hard_check(client, action_type="ANSWER", _safety_fn=_safe)
    assert as_ask.penalty == LENGTH_PENALTY
    assert as_answer.penalty == 1.0
    assert as_ask.fatal is None  # длина штрафует, а не обнуляет


def test_short_answer_passes_clean():
    result = hard_check("Клиенту: перезагрузите кассу Атол", _safety_fn=_safe)
    assert result.fatal is None and result.penalty == 1.0


# --- парные дельты ----------------------------------------------------------

def test_paired_deltas_only_shared_cases():
    base = {"a": 0.5, "b": 0.4, "c": 0.9}
    cand = {"a": 0.6, "b": 0.5, "d": 0.1}
    assert paired_deltas(base, cand) == [pytest.approx(0.1), pytest.approx(0.1)]


def test_bootstrap_is_reproducible():
    deltas = [0.05, 0.01, 0.08, -0.01, 0.03, 0.04]
    assert bootstrap_ci_low(deltas) == bootstrap_ci_low(deltas)


def test_bootstrap_none_on_single_case():
    assert bootstrap_ci_low([0.1]) is None


def test_consistent_improvement_passes():
    base = {str(i): 0.50 for i in range(10)}
    cand = {str(i): 0.58 for i in range(10)}
    verdict = apply_gate({"cases": base, "axes": {}}, {"cases": cand, "axes": {}})
    assert verdict["passed"] is True
    assert verdict["mean_delta"] == pytest.approx(0.08)


def test_positive_mean_but_wide_spread_fails():
    """Одно кейс вырос сильно, остальные просели — среднее плюс, гейт нет."""
    base = {str(i): 0.50 for i in range(8)}
    cand = {"0": 1.00, "1": 0.42, "2": 0.44, "3": 0.41,
            "4": 0.46, "5": 0.40, "6": 0.45, "7": 0.43}
    verdict = apply_gate({"cases": base, "axes": {}}, {"cases": cand, "axes": {}})
    assert verdict["mean_delta"] > 0
    assert verdict["passed"] is False
    assert any("не согласовано" in r for r in verdict["reasons"])


def test_tiny_improvement_below_minimum_fails():
    base = {str(i): 0.50 for i in range(10)}
    cand = {str(i): 0.505 for i in range(10)}
    verdict = apply_gate({"cases": base, "axes": {}}, {"cases": cand, "axes": {}})
    assert verdict["passed"] is False
    assert any("средняя дельта" in r for r in verdict["reasons"])


def test_too_few_paired_cases_fails():
    verdict = apply_gate(
        {"cases": {"a": 0.4, "b": 0.4}, "axes": {}},
        {"cases": {"a": 0.9, "b": 0.9}, "axes": {}},
    )
    assert verdict["passed"] is False
    assert any("мало парных кейсов" in r for r in verdict["reasons"])


# --- пер-осевые регрессии ---------------------------------------------------

def test_critical_axis_regression_blocks_apply():
    """Диагноз просел — apply запрещён, даже если общее среднее выросло."""
    base = {str(i): 0.50 for i in range(10)}
    cand = {str(i): 0.60 for i in range(10)}
    verdict = apply_gate(
        {"cases": base, "axes": {"diagnosis": 0.80, "actionability": 0.70}},
        {"cases": cand, "axes": {"diagnosis": 0.60, "actionability": 0.72}},
    )
    assert verdict["passed"] is False
    assert verdict["regressions"] and "diagnosis" in verdict["regressions"][0]


def test_noncritical_axis_drop_is_tolerated():
    base = {str(i): 0.50 for i in range(10)}
    cand = {str(i): 0.60 for i in range(10)}
    verdict = apply_gate(
        {"cases": base, "axes": {"diagnosis": 0.8, "human": 0.9}},
        {"cases": cand, "axes": {"diagnosis": 0.8, "human": 0.5}},
    )
    assert verdict["passed"] is True  # стиль не критичен, диагноз держится


def test_axis_regression_ignores_missing_axes():
    assert axis_regressions({"diagnosis": None}, {"diagnosis": 0.1}) == []
    assert axis_regressions({}, {}) == []
