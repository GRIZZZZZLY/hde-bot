"""Tests for Приоритет/Тип autofill: combo table, prompt, parser,
classifier, HDE client extension, DB columns, apply wiring, outcome log."""
import pytest

from bot import db as _db


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


# --- combo table & parser ---

def test_pt_combos_cover_four_rules():
    from bot.ticket_fields import _PT_COMBOS
    assert set(_PT_COMBOS) == {"1", "2", "3", "4"}
    # ускоренный+ошибка, стандарт+ошибка, ускоренный+задача, стандарт+задача.
    # «Низкий» бот не ставит — только отдел оборудования вручную.
    assert _PT_COMBOS["1"] == ("1", "3")
    assert _PT_COMBOS["2"] == ("10", "3")
    assert _PT_COMBOS["3"] == ("1", "2")
    assert _PT_COMBOS["4"] == ("10", "2")


def test_parse_pt_combo_plain_number():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("1") == ("1", "3")


def test_parse_pt_combo_with_reasoning_text():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("Касса не работает, торговля стоит. Ответ: 1") == ("1", "3")


def test_parse_pt_combo_undetermined():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("НЕ ОПРЕДЕЛЕНО") is None


def test_parse_pt_combo_out_of_range_rejected():
    from bot.ticket_fields import _parse_pt_combo
    # 5-7 и 10 — не номера комбинаций; "10" не должен распадаться на "1"
    assert _parse_pt_combo("5") is None
    assert _parse_pt_combo("6") is None
    assert _parse_pt_combo("7") is None
    assert _parse_pt_combo("10") is None


def test_parse_pt_combo_empty():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("") is None


def test_pt_prompt_contains_all_combos_and_undetermined():
    from bot.ticket_fields import _build_pt_prompt
    p = _build_pt_prompt()
    for num in ("1 =", "2 =", "3 =", "4 ="):
        assert num in p
    assert "5 =" not in p and "6 =" not in p
    assert "НЕ ОПРЕДЕЛЕНО" in p
    assert "инженера банка" in p.lower() or "инженер банка" in p.lower()


# --- classify_priority_type ---

@pytest.mark.asyncio
async def test_classify_pt_returns_combo(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock(return_value="1")
    monkeypatch.setattr(tf, "_groq_classify", groq)
    result = await tf.classify_priority_type(
        "Клиент: касса не включается, продавать не можем",
        ticket_title="Касса не работает",
    )
    assert result == ("1", "3")
    prompt_arg, user_content = groq.await_args.args
    assert "НЕ ОПРЕДЕЛЕНО" in prompt_arg
    assert "Тема тикета: Касса не работает" in user_content
    assert "Переписка:\nКлиент: касса не включается" in user_content


@pytest.mark.asyncio
async def test_classify_pt_falls_back_to_scout(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock(side_effect=[None, "4"])
    monkeypatch.setattr(tf, "_groq_classify", groq)
    result = await tf.classify_priority_type("Клиент: настройте принтер")
    assert result == ("10", "2")
    assert groq.await_count == 2
    assert groq.await_args_list[1].kwargs["model"] == __import__('bot.config', fromlist=['config']).config.groq_classify_fallback_model


@pytest.mark.asyncio
async def test_classify_pt_undetermined(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    monkeypatch.setattr(tf, "_groq_classify", AsyncMock(return_value="НЕ ОПРЕДЕЛЕНО"))
    assert await tf.classify_priority_type("Клиент: привет") is None


@pytest.mark.asyncio
async def test_classify_pt_empty_input_skips_llm(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock()
    monkeypatch.setattr(tf, "_groq_classify", groq)
    assert await tf.classify_priority_type("", ticket_title="") is None
    groq.assert_not_awaited()


# --- HDE client extension ---

def test_ticket_update_body_custom_only():
    from bot.hde_api import HDEApiClient
    assert HDEApiClient._ticket_update_body({"2": "145"}) == {"custom_fields": {"2": "145"}}


def test_ticket_update_body_with_priority_and_type():
    from bot.hde_api import HDEApiClient
    body = HDEApiClient._ticket_update_body({"2": "145"}, priority_id="1", type_id="0")
    # type_id=0 («Вопрос») falsy — обязан попасть в тело
    assert body == {"custom_fields": {"2": "145"}, "priority_id": 1, "type_id": 0}


def test_ticket_update_body_priority_only():
    from bot.hde_api import HDEApiClient
    body = HDEApiClient._ticket_update_body({}, priority_id="10")
    assert body == {"custom_fields": {}, "priority_id": 10}


@pytest.mark.asyncio
async def test_get_ticket_priority_type_parses_ids(monkeypatch):
    from bot.hde_api import HDEApiClient
    from unittest.mock import AsyncMock

    c = HDEApiClient.__new__(HDEApiClient)
    c.base_url = "https://x"
    c._get = AsyncMock(return_value=(200, {"data": {"priority_id": 10, "type_id": 0}}))
    assert await c.get_ticket_priority_type("T1") == ("10", "0")


@pytest.mark.asyncio
async def test_get_ticket_priority_type_error_returns_none(monkeypatch):
    from bot.hde_api import HDEApiClient
    from unittest.mock import AsyncMock

    c = HDEApiClient.__new__(HDEApiClient)
    c.base_url = "https://x"
    c._get = AsyncMock(return_value=(500, {}))
    assert await c.get_ticket_priority_type("T1") is None


# --- DB columns ---

@pytest.mark.asyncio
async def test_pt_option_ids_roundtrip():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    await _db.update_topic("t1", priority_option_id="1", type_option_id="0")
    rec = await _db.get_topic("t1")
    assert rec.priority_option_id == "1"
    assert rec.type_option_id == "0"


@pytest.mark.asyncio
async def test_pt_option_ids_default_none():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    rec = await _db.get_topic("t1")
    assert rec.priority_option_id is None
    assert rec.type_option_id is None


# --- apply_ticket_fields wiring ---

def _fake_record(photo_descriptions="", company_name="", env_option_id=None, ticket_name=""):
    from unittest.mock import MagicMock
    rec = MagicMock()
    rec.photo_descriptions = photo_descriptions
    rec.company_name = company_name
    rec.env_option_id = env_option_id
    rec.ticket_name = ticket_name
    return rec


def _patched_apply_env(monkeypatch, tf):
    """Общая обвязка: HDE-клиент, env-классификатор, БД-моки."""
    from unittest.mock import AsyncMock, MagicMock
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=199)
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value="146"))
    monkeypatch.setattr(_db, "get_topic", AsyncMock(return_value=None))
    update_topic = AsyncMock()
    monkeypatch.setattr(_db, "update_topic", update_topic)
    return fake_client, update_topic


@pytest.mark.asyncio
async def test_apply_sets_priority_and_type(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client, update_topic = _patched_apply_env(monkeypatch, tf)
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=("1", "3")))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: касса встала")

    assert res.priority_id == "1"
    assert res.type_id == "3"
    kwargs = fake_client.update_ticket_fields.await_args.kwargs
    assert kwargs["priority_id"] == "1"
    assert kwargs["type_id"] == "3"
    # предсказание сохранено в БД
    pt_call = [c for c in update_topic.await_args_list if "priority_option_id" in c.kwargs]
    assert pt_call and pt_call[0].kwargs["priority_option_id"] == "1"
    assert pt_call[0].kwargs["type_option_id"] == "3"
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_pt_undetermined_warns_and_skips_fields(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client, update_topic = _patched_apply_env(monkeypatch, tf)
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=None))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: привет")

    assert res.priority_id is None and res.type_id is None
    kwargs = fake_client.update_ticket_fields.await_args.kwargs
    assert kwargs["priority_id"] is None
    assert kwargs["type_id"] is None
    # '' = «не определено» в БД
    pt_call = [c for c in update_topic.await_args_list if "priority_option_id" in c.kwargs]
    assert pt_call and pt_call[0].kwargs["priority_option_id"] == ""
    assert pt_call[0].kwargs["type_option_id"] == ""
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert any("Приоритет и тип не определены" in t for t in texts)


@pytest.mark.asyncio
async def test_apply_pt_type_vopros_zero_reaches_put(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client, update_topic = _patched_apply_env(monkeypatch, tf)
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=("3", "0")))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: как сделать X?")

    # «Вопрос» = "0" не должен потеряться из-за falsy-проверок
    assert res.type_id == "0"
    assert fake_client.update_ticket_fields.await_args.kwargs["type_id"] == "0"
    pt_call = [c for c in update_topic.await_args_list if "priority_option_id" in c.kwargs]
    assert pt_call[0].kwargs["type_option_id"] == "0"


# --- log_pt_outcome ---

@pytest.mark.asyncio
async def test_log_pt_outcome_writes_jsonl(tmp_path, monkeypatch):
    import json
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_priority_type = AsyncMock(return_value=("10", "2"))
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    out = tmp_path / "priority_corrections.jsonl"
    monkeypatch.setattr(tf, "PT_CORRECTIONS_PATH", str(out))

    await tf.log_pt_outcome("T1", "1", "3")

    entry = json.loads(out.read_text(encoding="utf-8").strip())
    assert entry["ticket_id"] == "T1"
    assert entry["predicted_priority"] == "1"
    assert entry["predicted_type"] == "3"
    assert entry["final_priority"] == "10"
    assert entry["final_type"] == "2"
    assert entry["match"] is False


@pytest.mark.asyncio
async def test_log_pt_outcome_match_with_type_zero(tmp_path, monkeypatch):
    import json
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_priority_type = AsyncMock(return_value=("3", "0"))
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    out = tmp_path / "priority_corrections.jsonl"
    monkeypatch.setattr(tf, "PT_CORRECTIONS_PATH", str(out))

    await tf.log_pt_outcome("T1", "3", "0")

    entry = json.loads(out.read_text(encoding="utf-8").strip())
    assert entry["match"] is True


@pytest.mark.asyncio
async def test_log_pt_outcome_never_raises(monkeypatch):
    import bot.ticket_fields as tf

    def boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(tf, "HDEApiClient", boom)
    await tf.log_pt_outcome("T1", "1", "3")  # не должно бросить


# --- topic_manager close hook ---

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
async def test_ticket_closed_logs_pt_outcome(monkeypatch):
    import bot.topic_manager as tm
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    await _db.init_db()
    await _db.upsert_topic("TKT-1", 999)
    await _db.update_topic("TKT-1", priority_option_id="1", type_option_id="3")

    monkeypatch.setattr(tf, "log_env_outcome", AsyncMock())
    log = AsyncMock()
    monkeypatch.setattr(tf, "log_pt_outcome", log)
    monkeypatch.setattr(tm, "_delete_topic_now", AsyncMock(return_value=True))
    monkeypatch.setattr(tm, "_try_delete_pre_sla_message", AsyncMock())

    bot = AsyncMock()
    await tm.handle_ticket_closed(bot, _tm_payload(status="closed"))
    log.assert_awaited_once_with("TKT-1", "1", "3")


@pytest.mark.asyncio
async def test_ticket_closed_skips_pt_log_when_never_classified(monkeypatch):
    import bot.topic_manager as tm
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    await _db.init_db()
    await _db.upsert_topic("TKT-1", 999)  # priority_option_id остаётся NULL

    monkeypatch.setattr(tf, "log_env_outcome", AsyncMock())
    log = AsyncMock()
    monkeypatch.setattr(tf, "log_pt_outcome", log)
    monkeypatch.setattr(tm, "_delete_topic_now", AsyncMock(return_value=True))
    monkeypatch.setattr(tm, "_try_delete_pre_sla_message", AsyncMock())

    bot = AsyncMock()
    await tm.handle_ticket_closed(bot, _tm_payload(status="closed"))
    log.assert_not_awaited()
