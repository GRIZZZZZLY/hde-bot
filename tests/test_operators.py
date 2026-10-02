"""Operator registry: primary from .env, colleagues from OPERATORS_FILE."""
import json

from bot import operators
from bot.config import config


def _write_registry(tmp_path, entries):
    path = tmp_path / "operators.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_registry_loads_colleague_with_own_key(monkeypatch, tmp_path):
    key = tmp_path / "hde_key_102"
    key.write_text("maxim@example.com:secret\n", encoding="utf-8")
    monkeypatch.setattr(config, "hde_owner_id", "98")
    monkeypatch.setenv("OPERATORS_FILE", _write_registry(tmp_path, [
        {"hde_id": 102, "name": "Максим Яницкий", "tg_user_id": 1220214456,
         "chat_id": -1004375098325, "key_file": str(key)},
        {"hde_id": 98, "name": "дубль основного", "tg_user_id": 1, "chat_id": -1},
    ]))
    monkeypatch.setattr(operators, "COLLEAGUES", operators._load_colleagues())
    loaded = operators.all_operators()
    assert [o.hde_id for o in loaded] == ["98", "102"]  # primary first, its duplicate dropped
    maxim = loaded[1]
    assert maxim.api_auth == "maxim@example.com:secret"
    assert maxim.first_name == "Максим"
    assert maxim.ai_enabled is False  # AI is opt-in for colleagues


def test_broken_registry_keeps_primary(monkeypatch, tmp_path):
    path = tmp_path / "operators.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("OPERATORS_FILE", str(path))
    assert operators._load_colleagues() == ()
    assert operators.all_operators()[0].ai_enabled


def test_missing_registry_is_single_operator(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATORS_FILE", str(tmp_path / "absent.json"))
    assert operators._load_colleagues() == ()


def test_lookups(monkeypatch):
    monkeypatch.setattr(config, "hde_owner_id", "98")
    monkeypatch.setattr(config, "hde_owner_name", "Игорь Кравцов")
    monkeypatch.setattr(config, "group_chat_id", -100111)
    maxim = operators.Operator("102", "Максим Яницкий", 777, -100222)
    monkeypatch.setattr(operators, "COLLEAGUES", (maxim,))
    igor = operators.primary()
    assert operators.by_owner("102", "Максим") is maxim       # id wins over a first-name-only payload
    assert operators.by_owner("", "максим яницкий") is maxim  # name only as a fallback
    assert operators.by_owner("61", "Юрий") is None
    assert operators.by_owner("", "") is None
    assert operators.by_tg_user(777) is maxim
    assert operators.by_chat(-100111) == igor
    assert config.is_operator_allowed(777)
    assert config.is_known_chat(-100222)
    assert not config.is_known_chat(-100999)


def test_hde_client_uses_the_acting_operators_key(monkeypatch):
    from bot.hde_api import HDEApiClient
    maxim = operators.Operator("102", "Максим Яницкий", 777, -100222, api_auth="maxim@example.com:k:with:colons")
    monkeypatch.setattr(operators, "COLLEAGUES", (maxim,))
    own = HDEApiClient(auth=operators.hde_auth_for_user(777)).auth
    assert (own.login, own.password) == ("maxim@example.com", "k:with:colons")
    assert operators.hde_auth_for_owner("102") == maxim.api_auth
    shared = HDEApiClient(auth=operators.hde_auth_for_user(config.personal_chat_id)).auth
    assert (shared.login, shared.password) == (config.hde_api_email, config.hde_api_key)
    assert HDEApiClient(auth="broken").auth.login == config.hde_api_email
