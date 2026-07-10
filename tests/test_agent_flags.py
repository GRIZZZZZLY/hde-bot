import bot.config as config_module


def test_agent_flags_present_with_safe_defaults():
    cfg = config_module.config
    # kill switch and feature gates exist and default off (no behavior change)
    assert hasattr(cfg, "agent_enabled")
    assert hasattr(cfg, "agent_pipeline_version")
    assert hasattr(cfg, "agent_auto_first_suggestion_enabled")
    assert hasattr(cfg, "agent_dynamic_fewshot_enabled")
    assert hasattr(cfg, "agent_call_fixation_enabled")
    assert isinstance(cfg.agent_pipeline_version, str) and cfg.agent_pipeline_version


def test_agent_enabled_parses_env(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_PIPELINE_VERSION", "v1")
    fresh = config_module.Config.from_env()
    assert fresh.agent_enabled is True
    assert fresh.agent_pipeline_version == "v1"
