import bot.config as config_module


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
