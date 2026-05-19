from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.hde_api import HDEApiClient
from bot.ticket_fields import (
    FIELD_OKRUZHENIE,
    FIELD_KLASSIFIKACIYA,
    FIELD_ROL,
    KLASSIFIKACIYA_OBORUDOVANIE,
    ROL_NE_VAZHNO,
    OKRUZHENIE_OPTIONS,
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


def _gemini_resp(text: str):
    """Build a fake aiohttp response context manager returning Gemini JSON."""
    payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
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
    session.post = MagicMock(return_value=_gemini_resp("11"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.config.gemini_api_key", "test-key"), \
         patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("Клиент: не открывается касса POS")
    assert result == "11"


@pytest.mark.asyncio
async def test_classify_environment_undetermined():
    session = MagicMock()
    session.post = MagicMock(return_value=_gemini_resp("НЕ ОПРЕДЕЛЕНО"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.config.gemini_api_key", "test-key"), \
         patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("Клиент: добрый день")
    assert result is None


@pytest.mark.asyncio
async def test_classify_environment_unknown_id_returns_none():
    session = MagicMock()
    session.post = MagicMock(return_value=_gemini_resp("99999"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.config.gemini_api_key", "test-key"), \
         patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("текст")
    assert result is None


def _hde_get_resp(custom_fields: list):
    payload = {"data": {"custom_fields": custom_fields}}
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value="")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, payload


def _patch_session(get_cm_and_payload):
    get_cm, payload = get_cm_and_payload
    session = MagicMock()
    session.get = MagicMock(return_value=get_cm)
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    from contextlib import contextmanager
    from unittest.mock import patch as _patch

    class _multi:
        def __enter__(self):
            self._p1 = _patch("bot.hde_api.aiohttp.ClientSession", return_value=sess_cm)
            self._p2 = _patch.object(
                HDEApiClient, "_read_response", new=AsyncMock(return_value=payload)
            )
            self._p1.__enter__()
            self._p2.__enter__()
            return self

        def __exit__(self, *args):
            self._p2.__exit__(*args)
            self._p1.__exit__(*args)

    return _multi()


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
