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
    assert fresh.agent_draft_model == "llama-3.3-70b-versatile"
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


def test_action_instruction_forbids_remote_access_first():
    from bot.agent.actions import build_action_instruction
    text = build_action_instruction().lower()
    assert "удалённый доступ" in text or "удаленный доступ" in text
    assert "перв" in text                        # «не первым шагом»
