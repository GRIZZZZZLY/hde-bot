"""Auto-fill HDE ticket custom fields after AI summary.

Field & option IDs discovered empirically by scanning 450 production
tickets (HDE API does not expose select-field option lists).
"""
from __future__ import annotations

import logging
import re

import aiohttp

from .config import config

logger = logging.getLogger(__name__)

# Custom field IDs (HDE /custom_fields/ endpoint)
FIELD_OKRUZHENIE = "2"
FIELD_KLASSIFIKACIYA = "3"
FIELD_ROL = "24"

# Fixed option IDs
KLASSIFIKACIYA_OBORUDOVANIE = "20"  # "Оборудование"
ROL_NE_VAZHNO = "197"               # "Не важно"

# Окружение: option_id -> human label (field_id 2)
OKRUZHENIE_OPTIONS: dict[str, str] = {
    "15": "Интернет витрина",
    "11": "POS",
    "146": "Эвотор",
    "148": "АКСИ \\ AQSI",
    "12": "Биллинг",
    "190": "Wallet",
    "10": "Админ панель",
    "145": "Атол",
    "17": "API",
    "140": "ТГ-Бот",
    "13": "Florist",
    "151": "Яндекс Пэй",
    "150": "INPAS \\ ИНПАС",
    "46": "Менеджер",
    "149": "SBER \\ СБЕР",
    "14": "Другое",
    "153": "Принтер Этикеток",
    "56": "Интеграция",
    "152": "Принтер Чеков",
    "157": "WEB касса \\ Веб касса",
    "156": "Viki Print \\ Вики принт",
}

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)


def _build_env_prompt() -> str:
    lines = "\n".join(
        f"{oid} = {label}" for oid, label in OKRUZHENIE_OPTIONS.items()
    )
    return (
        "Ты классифицируешь обращение в техподдержку кассового ПО/оборудования "
        "по полю «Окружение».\n"
        "Ниже список допустимых значений в формате «ID = Название»:\n\n"
        f"{lines}\n\n"
        "Прочитай переписку и определи наиболее подходящее окружение.\n"
        "Ответь СТРОГО одним токеном:\n"
        "— числовой ID из списка выше, ЕСЛИ окружение уверенно определяется;\n"
        "— либо ровно «НЕ ОПРЕДЕЛЕНО», если определить нельзя.\n"
        "Без пояснений, без префиксов, только токен."
    )


async def classify_environment(history: str) -> str | None:
    """Return an Окружение option_id, or None if undetermined / unknown / error."""
    if not config.gemini_api_key or not history.strip():
        return None
    payload = {
        "system_instruction": {"parts": [{"text": _build_env_prompt()}]},
        "contents": [{"parts": [{"text": f"Переписка:\n{history}"}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 16},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _GEMINI_URL,
                json=payload,
                params={"key": config.gemini_api_key},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Env classifier HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        logger.warning("Env classifier failed: %s", exc)
        return None
    m = re.fullmatch(r"\d+", raw)
    if not m:
        return None
    option_id = m.group(0)
    if option_id in OKRUZHENIE_OPTIONS:
        return option_id
    logger.info("Env classifier returned unknown id %r (raw=%r)", option_id, raw[:60])
    return None
