"""Tests for Окружение autofill improvements: env_option_id storage,
company prior, keyword pre-pass, enriched classification context."""
import pytest

from bot import db as _db
from bot.ticket_fields import _keyword_match


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


# --- env_option_id column ---

@pytest.mark.asyncio
async def test_env_option_id_roundtrip():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    await _db.update_topic("t1", env_option_id="145")
    rec = await _db.get_topic("t1")
    assert rec.env_option_id == "145"


@pytest.mark.asyncio
async def test_env_option_id_default_none():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    rec = await _db.get_topic("t1")
    assert rec.env_option_id is None


# --- company prior ---

@pytest.mark.asyncio
async def test_get_common_env_for_company_most_frequent():
    await _db.init_db()
    for i, env in enumerate(["146", "146", "145"]):
        await _db.upsert_topic(f"t{i}", 100 + i)
        await _db.update_topic(f"t{i}", company_name="ООО Ромашка", env_option_id=env)
    await _db.upsert_topic("t9", 999)
    await _db.update_topic("t9", company_name="ООО Ромашка")
    assert await _db.get_common_env_for_company("ООО Ромашка", exclude_ticket_id="t9") == "146"


@pytest.mark.asyncio
async def test_get_common_env_for_company_excludes_current_and_empty():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    await _db.update_topic("t1", company_name="ООО Ромашка", env_option_id="")
    assert await _db.get_common_env_for_company("ООО Ромашка", exclude_ticket_id="t2") is None
    assert await _db.get_common_env_for_company("", exclude_ticket_id="t2") is None


# --- keyword pre-pass ---

def test_keyword_match_single_brand():
    assert _keyword_match("касса атол не печатает чек") == "145"


def test_keyword_match_latin_and_case():
    assert _keyword_match("Проблема с Evotor после обновления") == "146"


def test_keyword_match_two_brands_ambiguous():
    assert _keyword_match("эвотор подключён к атол") is None


def test_keyword_match_shtrihkod_not_shtrih():
    # «штрихкод»/«штрих-код» не должны давать ККТ Штрих; «сканер» решает
    assert _keyword_match("сканер штрихкодов не читает штрих-код") == "154"


def test_keyword_match_shtrih_kkt():
    assert _keyword_match("ккт штрих не фискализирует") == "155"


def test_keyword_match_sber_requires_terminal_context():
    # просто упоминание Сбера (оплата, приложение) — не эквайринговый терминал
    assert _keyword_match("клиент оплатил через сбер онлайн") is None
    assert _keyword_match("терминал сбер не проводит оплату") == "149"


def test_keyword_match_nothing():
    assert _keyword_match("не работает программа, помогите") is None


# --- classify_environment: pre-pass, title, prior ---

@pytest.mark.asyncio
async def test_classify_keyword_shortcut_skips_llm(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock()
    monkeypatch.setattr(tf, "_groq_classify", groq)
    # keyword из заголовка работает даже без Groq-ключа
    monkeypatch.setattr(tf.config, "groq_api_key", "")
    result = await tf.classify_environment("Клиент: не печатает", ticket_title="Атол 30Ф ошибка")
    assert result == "145"
    groq.assert_not_awaited()


# --- apply_ticket_fields: enrichment + env storage ---

def _fake_record(photo_descriptions="", company_name="", env_option_id=None, ticket_name=""):
    from unittest.mock import MagicMock
    rec = MagicMock()
    rec.photo_descriptions = photo_descriptions
    rec.company_name = company_name
    rec.env_option_id = env_option_id
    rec.ticket_name = ticket_name
    return rec


@pytest.mark.asyncio
async def test_apply_enriches_history_and_stores_env(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=199)
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)

    classify = AsyncMock(return_value="146")
    monkeypatch.setattr(tf, "classify_environment", classify)
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=None))

    monkeypatch.setattr(
        _db, "get_topic",
        AsyncMock(return_value=_fake_record(
            photo_descriptions="смарт-терминал Эвотор, ошибка на экране",
            company_name="ООО Ромашка",
        )),
    )
    monkeypatch.setattr(_db, "get_common_env_for_company", AsyncMock(return_value="146"))
    update_topic = AsyncMock()
    monkeypatch.setattr(_db, "update_topic", update_topic)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(
        bot, "T1", 555, "Клиент: касса зависла", ticket_title="Зависла касса"
    )

    assert res.env_id == "146"
    history_arg, title_arg, prior_arg = classify.await_args.args
    assert "[Описание фото из тикета: смарт-терминал Эвотор" in history_arg
    assert title_arg == "Зависла касса"
    assert prior_arg == "Эвотор"
    update_topic.assert_any_await("T1", env_option_id="146")


@pytest.mark.asyncio
async def test_apply_stores_empty_env_when_undetermined(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=199)
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value=None))
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=None))
    monkeypatch.setattr(_db, "get_topic", AsyncMock(return_value=None))
    update_topic = AsyncMock()
    monkeypatch.setattr(_db, "update_topic", update_topic)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: привет")
    assert res.env_id is None
    update_topic.assert_any_await("T1", env_option_id="")


@pytest.mark.asyncio
async def test_apply_appends_audio_transcripts(monkeypatch):
    import bot.ticket_fields as tf
    import bot.ai_summary as ai
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=199)
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)

    classify = AsyncMock(return_value="145")
    monkeypatch.setattr(tf, "classify_environment", classify)
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=None))
    monkeypatch.setattr(_db, "get_topic", AsyncMock(return_value=None))
    monkeypatch.setattr(_db, "update_topic", AsyncMock())
    monkeypatch.setattr(ai, "_transcribe_audio_posts", AsyncMock(return_value=["алло, у нас касса атол"]))

    sess = MagicMock()
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=sess)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(tf, "shared_session", lambda: sess_cm)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: голосовое", posts=[MagicMock()])

    history_arg = classify.await_args.args[0]
    assert "[Голосовое сообщение клиента: алло, у нас касса атол]" in history_arg


# --- retry_env_classification ---

@pytest.mark.asyncio
async def test_retry_env_classification_success(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    post = MagicMock()
    post.date_created = "2026-06-12"
    fake_client = MagicMock()
    fake_client.get_ticket_info = AsyncMock(return_value=MagicMock())
    fake_client.get_ticket_posts = AsyncMock(return_value=[post])
    fake_client.get_ticket_comments = AsyncMock(return_value=[])
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)

    import bot.ai_summary as ai
    monkeypatch.setattr(ai, "_build_history_text", lambda posts, info: "Клиент: теперь атол")
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value="145"))
    monkeypatch.setattr(_db, "get_topic", AsyncMock(return_value=_fake_record(ticket_name="Касса")))
    update_topic = AsyncMock()
    monkeypatch.setattr(_db, "update_topic", update_topic)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await tf.retry_env_classification(bot, "T1", 555)

    fake_client.update_ticket_fields.assert_awaited_once_with("T1", {"2": "145"})
    update_topic.assert_awaited_once_with("T1", env_option_id="145")
    assert "Окружение определено" in bot.send_message.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_retry_env_classification_still_undetermined_is_silent(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_info = AsyncMock(return_value=MagicMock())
    fake_client.get_ticket_posts = AsyncMock(return_value=[])
    fake_client.get_ticket_comments = AsyncMock(return_value=[])
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    import bot.ai_summary as ai
    monkeypatch.setattr(ai, "_build_history_text", lambda posts, info: "Клиент: привет")
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value=None))
    monkeypatch.setattr(_db, "get_topic", AsyncMock(return_value=None))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await tf.retry_env_classification(bot, "T1", 555)

    fake_client.update_ticket_fields.assert_not_awaited()
    bot.send_message.assert_not_awaited()


# --- topic_manager hooks ---

def _tm_payload(**overrides):
    payload = {
        "ticket_id": "TKT-1",
        "unique_id": "ABC-123",
        "ticket_name": "Касса не печатает",
        "company_name": "ACME",
        "priority": "high",
        "status": "open",
        "owner_id": "me",
        "owner_name": "Me",
        "user_name": "Alice",
        "message": "Помогите",
        "last_post_date": "2026-06-12 12:00:00",
        "link": "https://hde.example.com/tickets/1",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_client_reply_schedules_env_retry_when_undetermined(monkeypatch):
    import asyncio
    import bot.topic_manager as tm
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    await _db.init_db()
    await _db.upsert_topic("TKT-1", 999, ticket_name="Касса", company_name="ACME")
    await _db.update_topic("TKT-1", env_option_id="")

    monkeypatch.setattr(tm, "_is_work_time", lambda: True)
    retry = AsyncMock()
    monkeypatch.setattr(tf, "retry_env_classification", retry)

    bot = AsyncMock()
    await tm.handle_client_reply(bot, _tm_payload())
    for _ in range(5):
        await asyncio.sleep(0)
    retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_reply_no_retry_when_env_already_set(monkeypatch):
    import asyncio
    import bot.topic_manager as tm
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    await _db.init_db()
    await _db.upsert_topic("TKT-1", 999, ticket_name="Касса", company_name="ACME")
    await _db.update_topic("TKT-1", env_option_id="146")

    monkeypatch.setattr(tm, "_is_work_time", lambda: True)
    retry = AsyncMock()
    monkeypatch.setattr(tf, "retry_env_classification", retry)

    bot = AsyncMock()
    await tm.handle_client_reply(bot, _tm_payload())
    for _ in range(5):
        await asyncio.sleep(0)
    retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_ticket_closed_logs_env_outcome(monkeypatch):
    import bot.topic_manager as tm
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    await _db.init_db()
    await _db.upsert_topic("TKT-1", 999)
    await _db.update_topic("TKT-1", env_option_id="145")

    log = AsyncMock()
    monkeypatch.setattr(tf, "log_env_outcome", log)
    monkeypatch.setattr(tm, "_delete_topic_now", AsyncMock(return_value=True))
    monkeypatch.setattr(tm, "_try_delete_pre_sla_message", AsyncMock())

    bot = AsyncMock()
    await tm.handle_ticket_closed(bot, _tm_payload(status="closed"))
    log.assert_awaited_once_with("TKT-1", "145")


@pytest.mark.asyncio
async def test_ticket_closed_skips_log_when_never_classified(monkeypatch):
    import bot.topic_manager as tm
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    await _db.init_db()
    await _db.upsert_topic("TKT-1", 999)  # env_option_id остаётся NULL

    log = AsyncMock()
    monkeypatch.setattr(tf, "log_env_outcome", log)
    monkeypatch.setattr(tm, "_delete_topic_now", AsyncMock(return_value=True))
    monkeypatch.setattr(tm, "_try_delete_pre_sla_message", AsyncMock())

    bot = AsyncMock()
    await tm.handle_ticket_closed(bot, _tm_payload(status="closed"))
    log.assert_not_awaited()


# --- log_env_outcome ---

@pytest.mark.asyncio
async def test_log_env_outcome_writes_jsonl(tmp_path, monkeypatch):
    import json
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=146)
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    out = tmp_path / "env_corrections.jsonl"
    monkeypatch.setattr(tf, "ENV_CORRECTIONS_PATH", str(out))

    await tf.log_env_outcome("T1", "145")

    entry = json.loads(out.read_text(encoding="utf-8").strip())
    assert entry["ticket_id"] == "T1"
    assert entry["predicted"] == "145"
    assert entry["final"] == "146"
    assert entry["match"] is False


@pytest.mark.asyncio
async def test_log_env_outcome_never_raises(monkeypatch):
    import bot.ticket_fields as tf

    def boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(tf, "HDEApiClient", boom)
    await tf.log_env_outcome("T1", "145")  # не должно бросить


@pytest.mark.asyncio
async def test_classify_title_and_prior_in_llm_content(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock(return_value="146")
    monkeypatch.setattr(tf, "_groq_classify", groq)
    result = await tf.classify_environment(
        "Клиент: касса зависла",
        ticket_title="Проблема с кассой",
        prior_hint="Эвотор",
    )
    assert result == "146"
    user_content = groq.await_args.args[1]
    assert "Тема тикета: Проблема с кассой" in user_content
    assert "Эвотор" in user_content
    assert "Переписка:\nКлиент: касса зависла" in user_content
