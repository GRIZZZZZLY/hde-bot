import bot.config as config_module


def test_staff_ids_and_mining_flag(monkeypatch):
    monkeypatch.setenv("AGENT_STAFF_USER_IDS", "10, 20 ,30")
    monkeypatch.setenv("AGENT_DIALOGUE_MINING_ENABLED", "true")
    fresh = config_module.Config.from_env()
    assert fresh.agent_staff_user_ids == ("10", "20", "30")
    assert fresh.agent_dialogue_mining_enabled is True


def test_mining_flag_default_off(monkeypatch):
    monkeypatch.delenv("AGENT_DIALOGUE_MINING_ENABLED", raising=False)
    assert config_module.Config.from_env().agent_dialogue_mining_enabled is False
