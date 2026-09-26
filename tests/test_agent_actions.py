import bot.config as config_module
from types import SimpleNamespace

from bot.agent.actions import (
    AGENT_ACTIONS,
    build_action_instruction,
    compose_memo,
    extract_client_text,
    parse_agent_draft,
)


def test_agent_model_config_defaults():
    fresh = config_module.Config.from_env()
    assert fresh.agent_draft_model == "qwen/qwen3.6-27b"
    assert fresh.agent_selfcheck_model == "openai/gpt-oss-120b"


def test_agent_model_config_env(monkeypatch):
    monkeypatch.setenv("AGENT_DRAFT_MODEL", "m1")
    monkeypatch.setenv("AGENT_SELFCHECK_MODEL", "m2")
    fresh = config_module.Config.from_env()
    assert fresh.agent_draft_model == "m1"
    assert fresh.agent_selfcheck_model == "m2"


def test_prompt_version_tag_legacy_by_default():
    import bot.ai_summary as ai
    ai._active_prompt_loaded = False
    ai._active_format_instructions = None
    assert ai.prompt_version_tag() == "legacy"
    ai._active_prompt_loaded = True
    ai._active_format_instructions = "custom"
    assert ai.prompt_version_tag() == "db-active"
    ai._active_prompt_loaded = False
    ai._active_format_instructions = None


def test_parse_agent_draft_ok_with_fences_and_reason():
    raw = ('```json\n{"action":"ASK","suit":"с","client":"какая модель?",'
           '"memo":"м","confidence":70,"confidence_reason":"нет модели кассы"}\n```')
    d = parse_agent_draft(raw)
    assert d["action"] == "ASK"
    assert d["confidence"] == 70
    assert d["confidence_reason"] == "нет модели кассы"


def test_parse_agent_draft_rejects_bad():
    assert parse_agent_draft('{"action":"MAYBE","suit":"s","client":"c","memo":"m"}') is None
    assert parse_agent_draft("не json") is None
    assert parse_agent_draft("") is None


def test_parse_agent_draft_defaults():
    d = parse_agent_draft('{"action":"ANSWER","suit":"s","client":"c","memo":"m"}')
    assert d["confidence"] == 50
    assert d["confidence_reason"] == ""


def test_build_action_instruction_lists_actions():
    instr = build_action_instruction()
    for a in AGENT_ACTIONS:
        assert a in instr


def test_extract_client_text_by_client_id():
    posts = [
        SimpleNamespace(user_id=1, text="<p>первый</p>"),
        SimpleNamespace(user_id=99, text="ответ оператора"),
        SimpleNamespace(user_id=1, text="<b>второй</b>"),
    ]
    assert extract_client_text(posts, client_id=1) == "второй"
    assert extract_client_text([], client_id=1) == ""


def test_compose_memo_body_and_stale():
    memo = compose_memo("перезагрузите кассу", stale_warning=True)
    assert memo.startswith("перезагрузите кассу")
    assert "нов" in memo.lower()  # предупреждение о новом сообщении клиента
    # служебные строки убраны — памятка не шумит
    assert "KB#" not in memo and "Действие:" not in memo and "Не хватает:" not in memo


def test_compose_memo_body_only():
    assert compose_memo("Атол 30Ф • драйверы clck.ru/x") == "Атол 30Ф • драйверы clck.ru/x"


def test_action_instruction_makes_remote_access_concrete():
    """Удалёнку больше не запрещаем (операторы так и работают) — требуем
    конкретики: какая программа, ссылка на неё и что прислать в ответ."""
    from bot.agent.actions import build_action_instruction
    text = build_action_instruction()
    low = text.lower()
    assert "удалённый доступ" in low or "удаленный доступ" in low
    assert "rudesktop.ru/downloads" in low and "anydesk.com" in low
    assert "номер рабочего места и пароль" in low   # что прислать для RuDesktop
    assert "статья" in low                          # статья впереди удалёнки


def test_action_instruction_bans_first_person_completed_actions():
    """«Подключаюсь к вам» / «Удалённо подключился» — оператор ещё ничего не
    сделал, а клиент ждёт несуществующего подключения (тикеты 194769, 195723)."""
    from bot.agent.actions import build_action_instruction, build_json_override
    combined = (build_action_instruction() + build_json_override()).lower()
    assert "первое лицо" in combined or "первого лица" in combined
    assert "подключаюсь" in combined and "подключился" in combined


def test_strip_reasoning_directive_removes_block_and_leadin():
    """Блок <reasoning> несовместим с JSON-выводом: модель его пишет, JSON не
    влезает в max_tokens и драфт падает на legacy (19 случаев в прод-логе)."""
    from bot.agent.actions import strip_reasoning_directive
    instructions = (
        "Опирайся на источники.\n\n"
        "Перед ответом заполни блок рассуждения (скрыт от пользователя):\n"
        "<reasoning>\n1. Что сломано?\n2. Какая модель?\n</reasoning>\n\n"
        "Формат ответа — ровно три метки:\n"
        "Суть: <одно предложение>\n"
    )
    out = strip_reasoning_directive(instructions)
    assert "<reasoning>" not in out
    assert "блок рассуждения" not in out
    assert "Опирайся на источники." in out       # остальное не тронуто
    assert "Суть: <одно предложение>" in out     # правила стиля остались


def test_strip_reasoning_directive_is_noop_without_block():
    from bot.agent.actions import strip_reasoning_directive
    text = "Формат ответа — ровно три метки.\nСуть: <...>"
    assert strip_reasoning_directive(text) == text


def test_parse_agent_draft_ignores_braces_inside_reasoning():
    """Жадный поиск JSON начинался с первой скобки в тексте — скобка внутри
    рассуждения уводила парсер мимо настоящего ответа."""
    from bot.agent.actions import parse_agent_draft
    raw = (
        "<reasoning>\nПрикинем {вариант A} и {вариант B}\n</reasoning>\n"
        '{"action": "ANSWER", "suit": "с", "client": "к", "memo": "м", '
        '"confidence": 70, "confidence_reason": "r"}'
    )
    draft = parse_agent_draft(raw)
    assert draft is not None
    assert draft["action"] == "ANSWER" and draft["client"] == "к"


def test_parse_draft_reads_analysis_and_source_ids():
    from bot.agent.actions import parse_agent_draft
    raw = ('{"analysis": "порт занят", "action": "ANSWER", "suit": "s", "client": "c", '
           '"memo": "m", "source_ids": ["KB#1", 5, "пара#2"], "confidence": 80}')
    d = parse_agent_draft(raw)
    assert d["analysis"] == "порт занят"
    assert d["source_ids"] == ["KB#1", "пара#2"]        # не-строки отброшены


def test_parse_draft_old_format_still_works():
    from bot.agent.actions import parse_agent_draft
    d = parse_agent_draft('{"action": "ASK", "suit": "", "client": "Какая модель?", "memo": "—"}')
    assert d["analysis"] == "" and d["source_ids"] == []


def test_prompt_version_tag_voice_v2(monkeypatch):
    import bot.ai_summary as ai
    from bot.config import config
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    assert ai.prompt_version_tag() == "voice-v2"


def test_compose_selfcheck_warning_separates_failure_from_verdict():
    """«Проверка сказала unsupported» и «проверка не отработала» — разные
    сообщения оператору: во втором случае факты вообще никто не смотрел."""
    from bot.agent.actions import compose_selfcheck_warning
    verdict = compose_selfcheck_warning(
        {"checked": True, "fallback_client_text": "уточните модель"}, "Атол 30Ф"
    )
    assert "без опоры на источники" in verdict
    assert "Запасной вариант: уточните модель" in verdict
    assert "Атол 30Ф" in verdict                 # памятка модели не потеряна

    broken = compose_selfcheck_warning({"checked": False, "fallback_client_text": ""}, "м")
    assert "не отработал" in broken
    assert "Запасной вариант" not in broken
