import pytest

import bot.config as config_module
import bot.db as db_module
import bot.hde_api as hde_api_module


@pytest.fixture(autouse=True)
def reset_embeddings_cache():
    """Drop the global embeddings cache around each test: every test gets its
    own tmp DB, so rows/matrix cached by a previous test are always stale."""
    import bot.knowledge.store as store_module
    store_module.invalidate_embeddings_cache()
    store_module._matrix_rows = None
    store_module._matrix = None
    store_module._matrix_norms = None
    yield
    store_module.invalidate_embeddings_cache()
    store_module._matrix_rows = None
    store_module._matrix = None
    store_module._matrix_norms = None


@pytest.fixture(autouse=True)
def reset_hde_connector():
    """Drop the shared HDE connector around each test so a connector bound to
    one test's event loop never leaks into the next."""
    hde_api_module._shared_connector = None
    hde_api_module._connector_loop = None
    yield
    hde_api_module._shared_connector = None
    hde_api_module._connector_loop = None


@pytest.fixture(autouse=True)
def set_test_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    return test_db


@pytest.fixture(autouse=True)
def configure_test_settings(monkeypatch):
    cfg = config_module.config
    monkeypatch.setattr(cfg, "group_chat_id", -100123456789)
    monkeypatch.setattr(cfg, "personal_chat_id", 123456789)
    monkeypatch.setattr(cfg, "hde_owner_id", "me")
    monkeypatch.setattr(cfg, "hde_owner_name", "Me")
    monkeypatch.setattr(cfg, "default_reply_sla_minutes", 30)
    monkeypatch.setattr(cfg, "pre_sla_warning_minutes", 10)
    monkeypatch.setattr(cfg, "scheduler_interval_seconds", 1)
    monkeypatch.setattr(cfg, "hde_webhook_secret", "test_secret")
    monkeypatch.setattr(cfg, "hde_api_base_url", "https://hde.example.com/api/v2")
    monkeypatch.setattr(cfg, "hde_api_email", "bot@example.com")
    monkeypatch.setattr(cfg, "hde_api_key", "secret_key")
    monkeypatch.setattr(cfg, "operator_telegram_user_ids", (123456789,))
    monkeypatch.setattr(cfg, "public_reply_enabled", True)
    monkeypatch.setattr(cfg, "public_reply_ticket_allowlist", ())
    monkeypatch.setattr(cfg, "digest_send_hour_utc", 5)
    monkeypatch.setattr(cfg, "digest_night_start_hour_utc", 15)
    monkeypatch.setattr(cfg, "general_topic_id", None)
    monkeypatch.setattr(cfg, "unassigned_department", "")
    return cfg


@pytest.fixture
async def initialized_db():
    await db_module.init_db()
    return db_module


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")
