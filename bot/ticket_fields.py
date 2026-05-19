"""Auto-fill HDE ticket custom fields after AI summary.

Field & option IDs discovered empirically by scanning 450 production
tickets (HDE API does not expose select-field option lists).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import aiohttp
from aiogram import Bot

from .config import config
from .hde_api import HDEApiClient

logger = logging.getLogger(__name__)

# Custom field IDs (HDE /custom_fields/ endpoint)
FIELD_OKRUZHENIE = "2"
FIELD_KLASSIFIKACIYA = "3"
FIELD_ROL = "24"

# Fixed option IDs
KLASSIFIKACIYA_OBORUDOVANIE = "20"  # "Оборудование"
ROL_NE_VAZHNO = "197"               # "Не важно"

# Окружение: option_id -> human label (field_id 2).
# Intentionally a restricted subset of HDE's 26 field-2 options — only
# these 13 are allowed for auto-classification (product decision). IDs
# verified against the authoritative HDE /custom_fields/2/options/ endpoint.
OKRUZHENIE_OPTIONS: dict[str, str] = {
    "14": "Другое",
    "145": "Атол",
    "146": "Эвотор",
    "147": "ККМ сервер",
    "148": "АКСИ \\ AQSI",
    "156": "Viki Print \\ Вики принт",
    "157": "WEB касса \\ Веб касса",
    "149": "SBER \\ СБЕР",
    "150": "INPAS \\ ИНПАС",
    "155": "Штрих",
    "152": "Принтер Чеков",
    "153": "Принтер Этикеток",
    "154": "Сканер",
}

# One-line disambiguation criteria per option (for the LLM prompt).
_OKRUZHENIE_CRITERIA: dict[str, str] = {
    "145": "фискальный регистратор / ККТ Атол",
    "146": "смарт-терминал Эвотор",
    "147": "серверная касса / ККМ-сервер (kkm-server, серверный фискальный модуль)",
    "148": "касса / фискальный регистратор АКСИ (AQSI)",
    "156": "фискальный принтер Viki Print (Вики Принт)",
    "157": "облачная веб-касса в браузере, без физического устройства",
    "149": "эквайринговый терминал Сбербанка (SBER)",
    "150": "эквайринговый терминал INPAS (ИНПАС)",
    "155": "ККТ / фискальный регистратор Штрих-М",
    "152": "нефискальный чековый принтер",
    "153": "принтер этикеток / штрихкодов",
    "154": "сканер штрихкодов",
    "14": "ничего из перечисленного выше не подходит",
}

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "llama-3.3-70b-versatile"
_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)


def _build_env_prompt() -> str:
    lines = "\n".join(
        f"{oid} = {OKRUZHENIE_OPTIONS[oid]} — {crit}"
        for oid, crit in _OKRUZHENIE_CRITERIA.items()
    )
    return (
        "Ты классифицируешь обращение в техподдержку кассового ПО/оборудования "
        "по полю «Окружение».\n"
        "Допустимые значения (формат «ID = Название — когда выбирать»):\n\n"
        f"{lines}\n\n"
        "Прочитай переписку, определи наиболее подходящее окружение строго из "
        "списка выше. Если уверенно определить нельзя — ответь «НЕ ОПРЕДЕЛЕНО».\n"
        "В конце ответа укажи только числовой ID выбранного значения "
        "(или «НЕ ОПРЕДЕЛЕНО»)."
    )


def _parse_env_id(raw: str) -> str | None:
    """Return the first whitelisted option_id found in the model output.

    Lenient: tolerates surrounding text/reasoning; rejects hallucinated
    ids not in OKRUZHENIE_OPTIONS; "НЕ ОПРЕДЕЛЕНО" yields no digits → None.
    """
    for tok in re.findall(r"\d+", raw):
        if tok in OKRUZHENIE_OPTIONS:
            return tok
    return None


async def _groq_classify(prompt: str, history: str) -> str | None:
    if not config.groq_api_key:
        return None
    payload = {
        "model": _GROQ_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Переписка:\n{history}"},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _GROQ_URL,
                json=payload,
                headers={"Authorization": f"Bearer {config.groq_api_key}"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Env classifier Groq HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("Env classifier Groq failed: %s", exc)
        return None


async def _gemini_classify(prompt: str, history: str) -> str | None:
    if not config.gemini_api_key:
        return None
    payload = {
        "system_instruction": {"parts": [{"text": prompt}]},
        "contents": [{"parts": [{"text": f"Переписка:\n{history}"}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 64},
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
                    logger.warning("Env classifier Gemini HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        logger.warning("Env classifier Gemini failed: %s", exc)
        return None


async def classify_environment(history: str) -> str | None:
    """Return an Окружение option_id, or None if undetermined / unknown / error.

    Groq first (fast); falls back to Gemini when Groq is unavailable
    (e.g. daily token limit / 429) so classification keeps working.
    """
    if not history.strip():
        return None
    prompt = _build_env_prompt()
    raw = await _groq_classify(prompt, history)
    if raw is None:
        raw = await _gemini_classify(prompt, history)
    if raw is None:
        return None
    option_id = _parse_env_id(raw)
    if option_id is None:
        logger.info("Env classifier: no valid id in response %r", raw[:80])
    return option_id


ENV_UNDETERMINED_MSG = "⚠️ Окружение не определено автоматически — выставьте вручную"


@dataclass
class AutofillResult:
    """Outcome of apply_ticket_fields, for callers that want feedback.

    updated: the HDE PUT succeeded.
    fields:  the custom_fields map that was sent (empty if not updated).
    env_id:  classified Окружение option_id, or None if undetermined.
    error:   human-readable reason when not updated, else None.
    """
    updated: bool
    fields: dict[str, str] = field(default_factory=dict)
    env_id: str | None = None
    error: str | None = None


async def apply_ticket_fields(
    bot: Bot, ticket_id: str, topic_id: int, history: str
) -> AutofillResult:
    """Auto-fill Классификация / Окружение / Роль after the AI summary.

    Never raises — any failure is logged so the summary flow is unaffected.
    Returns an AutofillResult; the auto-trigger hook ignores it.
    """
    try:
        client = HDEApiClient()
    except Exception as exc:
        logger.warning("apply_ticket_fields: no HDE client: %s", exc)
        return AutofillResult(updated=False, error="HDE API недоступен")

    fields: dict[str, str] = {FIELD_KLASSIFIKACIYA: KLASSIFIKACIYA_OBORUDOVANIE}

    # Роль: set only if currently empty (id == 0). None = read error → skip.
    try:
        current_rol = await client.get_ticket_field_value(ticket_id, int(FIELD_ROL))
    except Exception as exc:
        logger.warning("apply_ticket_fields: role read failed for %s: %s", ticket_id, exc)
        current_rol = None
    if current_rol == 0:
        fields[FIELD_ROL] = ROL_NE_VAZHNO

    # Окружение: LLM classify; overwrite always when determined.
    env_id = await classify_environment(history)
    if env_id:
        fields[FIELD_OKRUZHENIE] = env_id

    try:
        await client.update_ticket_fields(ticket_id, fields)
        logger.info("apply_ticket_fields: ticket %s updated %s", ticket_id, fields)
    except Exception as exc:
        logger.warning("apply_ticket_fields: update failed for %s: %s", ticket_id, exc)
        return AutofillResult(
            updated=False, fields=fields, env_id=env_id, error="ошибка записи в HDE"
        )

    if env_id is None:
        try:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=ENV_UNDETERMINED_MSG,
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning(
                "apply_ticket_fields: warn-message failed for topic %s: %s",
                topic_id, exc,
            )

    return AutofillResult(updated=True, fields=fields, env_id=env_id)
