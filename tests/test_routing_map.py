"""Карта ответственности: кто отвечает за проблему.

Приведённый владельцем случай — не факт для базы знаний, а правило маршрутизации:
«QR-код на терминале генерирует банк-эквайер, Posiflora к нему отношения не
имеет». Такие правила нужны в промпте ВСЕГДА, а не через retrieval: retrieval
молчит именно тогда, когда тема для базы новая, и бот в этот момент эскалирует
на специалиста, которым владелец и является.
"""
import json

from bot.agent.routing_map import (
    RoutingRule,
    format_routing_block,
    load_routing_map,
    parse_routing_map,
)


def test_parse_skips_incomplete_rules():
    rules = parse_routing_map([
        {"symptom": "QR на терминале", "owner": "банк", "say": "обратиться в банк"},
        {"symptom": "", "owner": "банк", "say": "x"},          # нет симптома
        {"symptom": "что-то", "owner": "", "say": "x"},        # нет адресата
        {"symptom": "что-то", "owner": "банк"},                # нет ответа
        "строка вместо объекта",
    ])
    assert [r.symptom for r in rules] == ["QR на терминале"]


def test_parse_normalises_owner():
    rules = parse_routing_map([
        {"symptom": "s1", "owner": "  Банк  ", "say": "a"},
        {"symptom": "s2", "owner": "ОФД", "say": "b"},
    ])
    assert [r.owner for r in rules] == ["банк", "ОФД"]


def test_format_block_lists_rules_with_owner_and_action():
    block = format_routing_block([
        RoutingRule(symptom="QR-код на платёжном терминале",
                    owner="банк", say="направить в банк, выпустивший терминал"),
    ])
    assert "QR-код на платёжном терминале" in block
    assert "банк" in block
    assert "направить в банк" in block


def test_format_block_tells_the_model_not_to_escalate():
    """Смысл блока — перебить привычку «передадим специалисту»: специалист и
    есть тот, кто читает черновик."""
    block = format_routing_block([
        RoutingRule(symptom="s", owner="банк", say="a"),
    ])
    assert "эскалир" in block.lower() or "не передавай" in block.lower()


def test_format_block_empty_without_rules():
    assert format_routing_block([]) == ""
    assert format_routing_block(None) == ""


def test_load_routing_map_missing_file_is_normal(tmp_path):
    """Карты нет — блок пустой, поведение как до правки. Это рабочий режим, а
    не ошибка: карта появляется после ручной вычитки."""
    assert load_routing_map(tmp_path / "нет-такого.json") == []


def test_load_routing_map_reads_json(tmp_path):
    path = tmp_path / "routing_map.json"
    path.write_text(json.dumps([
        {"symptom": "QR на терминале", "owner": "банк", "say": "в банк"},
        {"symptom": "чек не уходит в ОФД", "owner": "ОФД", "say": "в ОФД"},
    ], ensure_ascii=False), encoding="utf-8")
    rules = load_routing_map(path)
    assert len(rules) == 2 and rules[0].owner == "банк"


def test_load_routing_map_survives_broken_json(tmp_path):
    """Сломанный файл не должен ронять генерацию черновика."""
    path = tmp_path / "routing_map.json"
    path.write_text("{не json", encoding="utf-8")
    assert load_routing_map(path) == []


def test_load_routing_map_accepts_wrapped_object(tmp_path):
    path = tmp_path / "routing_map.json"
    path.write_text(json.dumps(
        {"rules": [{"symptom": "s", "owner": "банк", "say": "a"}]},
        ensure_ascii=False), encoding="utf-8")
    assert len(load_routing_map(path)) == 1


# --- блок в системном промпте ----------------------------------------------


def test_routing_block_lands_before_fewshot(monkeypatch):
    """Правило уровня политики должно стоять ДО стилевых примеров: few-shot
    сильнее инструкции, и правило, зажатое после примеров, проигрывает им."""
    import bot.ai_summary as ai

    monkeypatch.setattr(
        ai, "_load_routing_rules",
        lambda: [RoutingRule(symptom="QR на терминале", owner="банк", say="в банк")],
    )
    monkeypatch.setattr(ai, "_FEW_SHOT_EXAMPLES", [
        {"problem": "p", "suit": "s", "client": "c", "pamyatka": "m"},
    ])
    prompt = ai._build_system_prompt("Тема", format_instructions="FORMAT")
    assert "QR на терминале" in prompt
    assert prompt.index("QR на терминале") < prompt.index("Эталонные примеры")


def test_prompt_without_routing_map_is_unchanged(monkeypatch):
    import bot.ai_summary as ai

    monkeypatch.setattr(ai, "_load_routing_rules", lambda: [])
    prompt = ai._build_system_prompt("Тема", format_instructions="FORMAT")
    assert "Кто за что отвечает" not in prompt


def test_routing_map_failure_does_not_break_the_prompt(monkeypatch):
    import bot.ai_summary as ai

    def _boom():
        raise RuntimeError("файл повреждён")

    monkeypatch.setattr(ai, "_load_routing_rules", _boom)
    prompt = ai._build_system_prompt("Тема", format_instructions="FORMAT")
    assert "Тема" in prompt          # промпт собрался, карта просто не попала


# --- офлайн-выжимка карты --------------------------------------------------


def test_routing_lexicon_matches_real_operator_answers():
    from bot.agent.routing_map import looks_like_routing

    assert looks_like_routing(
        "В таком случае обращайтесь в банк, программа Posiflora никак не "
        "связанна с генерацией QR кода на терминале"
    )
    assert looks_like_routing("Это вопрос к вашему ОФД, обратитесь к ним")
    assert looks_like_routing("Обратитесь в техподдержку Атол, это их прошивка")
    assert not looks_like_routing("Перезагрузите кассу и попробуйте снова")
    assert not looks_like_routing("Обновил драйвер, пробуйте печатать чек")


def test_dedup_keeps_one_rule_per_symptom():
    from bot.agent.routing_map import dedup_rules

    rules = [
        RoutingRule(symptom="QR-код на терминале", owner="банк", say="в банк"),
        RoutingRule(symptom="qr-код на  терминале", owner="банк", say="в банк"),
        RoutingRule(symptom="чек не уходит в ОФД", owner="ОФД", say="в ОФД"),
    ]
    assert len(dedup_rules(rules)) == 2


def test_dedup_keeps_conflicting_owners_apart():
    """Один симптом с разными адресатами — не дубль, а противоречие, и молча
    склеивать его нельзя: вычитывающий человек должен его увидеть."""
    from bot.agent.routing_map import dedup_rules

    rules = [
        RoutingRule(symptom="терминал не проводит оплату", owner="банк", say="в банк"),
        RoutingRule(symptom="терминал не проводит оплату", owner="мы", say="решаем сами"),
    ]
    assert len(dedup_rules(rules)) == 2
