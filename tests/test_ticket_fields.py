from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bot.ticket_fields as tf
from bot.hde_api import HDEApiClient
from bot.ticket_fields import (
    FIELD_OKRUZHENIE,
    FIELD_KLASSIFIKACIYA,
    FIELD_ROL,
    KLASSIFIKACIYA_OBORUDOVANIE,
    ROL_NE_VAZHNO,
    OKRUZHENIE_OPTIONS,
    AutofillResult,
    classify_environment,
)


def test_field_ids_are_strings():
    assert FIELD_OKRUZHENIE == "2"
    assert FIELD_KLASSIFIKACIYA == "3"
    assert FIELD_ROL == "24"


def test_fixed_option_ids():
    assert KLASSIFIKACIYA_OBORUDOVANIE == "20"
    assert ROL_NE_VAZHNO == "197"


def test_okruzhenie_options_complete():
    # 21 environments discovered from production scan
    assert len(OKRUZHENIE_OPTIONS) == 21
    assert OKRUZHENIE_OPTIONS["11"] == "POS"
    assert OKRUZHENIE_OPTIONS["146"] == "Эвотор"
    assert OKRUZHENIE_OPTIONS["14"] == "Другое"
    # all keys are numeric strings
    assert all(k.isdigit() for k in OKRUZHENIE_OPTIONS)


def _groq_resp(text: str):
    """Build a fake aiohttp response context manager returning Groq JSON."""
    payload = {"choices": [{"message": {"content": text}}]}
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value="")
    resp.read = AsyncMock(return_value=b"")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_classify_environment_valid_id():
    session = MagicMock()
    session.post = MagicMock(return_value=_groq_resp("11"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.config.groq_api_key", "test-key"), \
         patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("Клиент: не открывается касса POS")
    assert result == "11"


@pytest.mark.asyncio
async def test_classify_environment_undetermined():
    session = MagicMock()
    session.post = MagicMock(return_value=_groq_resp("НЕ ОПРЕДЕЛЕНО"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.config.groq_api_key", "test-key"), \
         patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("Клиент: добрый день")
    assert result is None


@pytest.mark.asyncio
async def test_classify_environment_unknown_id_returns_none():
    session = MagicMock()
    session.post = MagicMock(return_value=_groq_resp("99999"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.config.groq_api_key", "test-key"), \
         patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("текст")
    assert result is None


def _hde_get_resp(custom_fields: list):
    payload = {"data": {"custom_fields": custom_fields}}
    resp = MagicMock()
    resp.status = 200
    resp.headers = {"Content-Type": "application/json"}
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value="")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _patch_session(get_cm):
    session = MagicMock()
    session.get = MagicMock(return_value=get_cm)
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    from unittest.mock import patch as _patch

    return _patch("bot.hde_api.aiohttp.ClientSession", return_value=sess_cm)


@pytest.mark.asyncio
async def test_get_ticket_field_value_empty():
    cf = [{"id": 24, "field_type": "select", "field_value": {"id": 0, "name": None}}]
    with _patch_session(_hde_get_resp(cf)):
        client = HDEApiClient()
        val = await client.get_ticket_field_value("123", 24)
    assert val == 0


@pytest.mark.asyncio
async def test_get_ticket_field_value_set():
    cf = [{"id": 24, "field_type": "select",
           "field_value": {"id": 199, "name": {"ru": "Администратор"}}}]
    with _patch_session(_hde_get_resp(cf)):
        client = HDEApiClient()
        val = await client.get_ticket_field_value("123", 24)
    assert val == 199


@pytest.mark.asyncio
async def test_get_ticket_field_value_missing_field():
    with _patch_session(_hde_get_resp([])):
        client = HDEApiClient()
        val = await client.get_ticket_field_value("123", 24)
    assert val == 0


@pytest.mark.asyncio
async def test_apply_env_found_role_empty(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=0)
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value="11"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: касса POS не печатает")

    fake_client.update_ticket_fields.assert_awaited_once_with(
        "T1", {"3": "20", "24": "197", "2": "11"}
    )
    bot.send_message.assert_not_called()
    assert res.updated is True
    assert res.env_id == "11"
    assert res.fields == {"3": "20", "24": "197", "2": "11"}
    assert res.error is None


@pytest.mark.asyncio
async def test_apply_env_undetermined_warns(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=199)  # Роль set
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value=None))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: здравствуйте")

    fake_client.update_ticket_fields.assert_awaited_once_with("T1", {"3": "20"})
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["message_thread_id"] == 555
    assert "Окружение не определено" in kwargs["text"]
    assert res.updated is True
    assert res.env_id is None
    assert res.fields == {"3": "20"}


@pytest.mark.asyncio
async def test_apply_role_unknown_state_skipped(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=None)  # read error
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value="146"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Эвотор завис")

    fake_client.update_ticket_fields.assert_awaited_once_with(
        "T1", {"3": "20", "2": "146"}
    )
    assert res.updated is True
    assert res.env_id == "146"
    assert res.fields == {"3": "20", "2": "146"}


@pytest.mark.asyncio
async def test_apply_update_failure_skips_warning(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=199)
    fake_client.update_ticket_fields = AsyncMock(side_effect=RuntimeError("HDE 500"))
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value=None))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    # Must not raise even though update_ticket_fields raised
    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: текст")

    fake_client.update_ticket_fields.assert_awaited_once()
    bot.send_message.assert_not_called()
    assert res.updated is False
    assert res.error
    assert res.env_id is None


def test_format_autofill_result_success_env_and_role():
    from bot.handlers.commands import _format_autofill_result

    r = AutofillResult(updated=True, fields={"3": "20", "24": "197", "2": "11"},
                        env_id="11")
    out = _format_autofill_result(r)
    assert "Поля тикета обновлены" in out
    assert "Классификация: Оборудование" in out
    assert "Окружение: POS" in out
    assert "Роль: Не важно" in out


def test_format_autofill_result_env_undetermined_role_unchanged():
    from bot.handlers.commands import _format_autofill_result

    r = AutofillResult(updated=True, fields={"3": "20"}, env_id=None)
    out = _format_autofill_result(r)
    assert "Окружение: ⚠️ не определено" in out
    assert "Роль: без изменений" in out


def test_format_autofill_result_not_updated():
    from bot.handlers.commands import _format_autofill_result

    r = AutofillResult(updated=False, error="ошибка записи в HDE")
    out = _format_autofill_result(r)
    assert "не выполнено" in out
    assert "ошибка записи в HDE" in out
