from bot.agent.safety import (
    PolicyDecision,
    post_generation_safety_check,
    pre_generation_policy_check,
)


def test_diagnosis_and_explanation_proceed():
    # объяснение/диагностика допустимы — не эскалируем
    for txt in [
        "что означает ошибка ОФД 231 на кассе",
        "почему не печатается чек, что проверить",
        "касса не подключается к интернету",
    ]:
        d = pre_generation_policy_check(txt)
        assert d.action == "PROCEED", txt


def test_escalation_categories():
    cases = {
        "нужно вернуть деньги клиенту за отменённый заказ": "finance",
        "надо перерегистрировать ККТ на нового владельца": "fiscal_change",
        "удалите все товары и продажи из базы": "data_loss",
        "смените пароль клиенту и выдайте доступ": "access",
    }
    for txt, category in cases.items():
        d = pre_generation_policy_check(txt)
        assert d.action == "ESCALATE", txt
        assert d.category == category, txt


def test_post_check_scans_generated_answer():
    d = post_generation_safety_check("Я сделаю возврат средств на вашу карту.")
    assert d.action == "ESCALATE"
    assert d.category == "finance"
    assert isinstance(d, PolicyDecision)
