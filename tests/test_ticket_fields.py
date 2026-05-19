from bot.ticket_fields import (
    FIELD_OKRUZHENIE,
    FIELD_KLASSIFIKACIYA,
    FIELD_ROL,
    KLASSIFIKACIYA_OBORUDOVANIE,
    ROL_NE_VAZHNO,
    OKRUZHENIE_OPTIONS,
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


from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.ticket_fields import classify_environment


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
