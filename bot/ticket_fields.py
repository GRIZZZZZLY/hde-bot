"""Auto-fill HDE ticket custom fields after AI summary.

Field & option IDs discovered empirically by scanning 450 production
tickets (HDE API does not expose select-field option lists).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

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

# Deterministic keyword pre-pass: (regex, option_id), проверяется до LLM.
# Срабатывает только при РОВНО одном упомянутом окружении (иначе решает LLM).
_KEYWORD_PATTERNS: list[tuple[str, str]] = [
    (r"\bатол\b|atol", "145"),
    (r"эвотор|evotor", "146"),
    (r"ккм[\s-]?сервер|kkm[\s-]?server", "147"),
    (r"\bакси\b|aqsi", "148"),
    (r"viki\s*print|вики\s*принт", "156"),
    (r"веб[\s-]?касс|web[\s-]?касс", "157"),
    # Сбер/ИНПАС считаем только в эквайринговом контексте, иначе слишком шумно
    (r"(?:терминал|эквайринг)[^.\n]{0,40}(?:сбер|sber)|(?:сбер|sber)[^.\n]{0,40}(?:терминал|эквайринг)", "149"),
    (r"inpas|инпас", "150"),
    (r"штрих(?![\s-]?код)|shtrih", "155"),
    (r"принтер[\s\w]{0,20}чеков|чековый\s+принтер", "152"),
    (r"принтер[\s\w]{0,20}этикеток", "153"),
    (r"сканер", "154"),
]


def _keyword_match(text: str) -> str | None:
    """Option id, если в тексте однозначно упомянуто ровно одно окружение."""
    low = text.lower()
    hits = {oid for pattern, oid in _KEYWORD_PATTERNS if re.search(pattern, low)}
    return hits.pop() if len(hits) == 1 else None


# --- Приоритет + Тип -------------------------------------------------------
# Стандартные поля тикета (не custom_fields). ID из справочников HDE
# GET /priorities/ и /types/ (проверено 2026-06-12).
PRIORITY_USKORENNY_OBORUD = "1"   # «Ускоренный 2я\оборуд»
PRIORITY_STANDART_OBORUD = "10"   # «Стандарт оборуд»
PRIORITY_NIZKIY_OBORUD = "3"      # «Низкий 2я\оборуд»

TYPE_VOPROS = "0"    # «Вопрос» — ноль валиден, проверять `is not None`!
TYPE_ZADACHA = "2"   # «Задача»
TYPE_OSHIBKA = "3"   # «Ошибка»

# Номер комбинации -> (priority_id, type_id). Бот никогда не ставит
# «Инцидент», «1я линия основной», «Стандарт 2я», «ИИ».
_PT_COMBOS: dict[str, tuple[str, str]] = {
    "1": (PRIORITY_USKORENNY_OBORUD, TYPE_OSHIBKA),
    "2": (PRIORITY_STANDART_OBORUD, TYPE_OSHIBKA),
    "3": (PRIORITY_USKORENNY_OBORUD, TYPE_ZADACHA),
    "4": (PRIORITY_STANDART_OBORUD, TYPE_ZADACHA),
    "5": (PRIORITY_NIZKIY_OBORUD, TYPE_VOPROS),
    "6": (PRIORITY_NIZKIY_OBORUD, TYPE_ZADACHA),
}


def _build_pt_prompt() -> str:
    return (
        "Ты классифицируешь обращение в техподдержку кассового ПО/оборудования "
        "по срочности (приоритет) и типу.\n"
        "Допустимые комбинации (формат «номер = Приоритет + Тип — когда выбирать»):\n\n"
        "1 = Ускоренный + Ошибка — торговля невозможна: не работает касса или "
        "платёжный терминал, продажи остановлены\n"
        "2 = Стандартный + Ошибка — что-то не работает, но торговля продолжается "
        "(например, не печатает принтер этикеток)\n"
        "3 = Ускоренный + Задача — приход инженера банка в магазин "
        "(визит специалиста, который не будет ждать очереди)\n"
        "4 = Стандартный + Задача — подключение, настройка или перенастройка "
        "оборудования (касса, принтер, другой формат этикетки, смена IP)\n"
        "5 = Низкий + Вопрос — вопрос или консультация, либо что-то непонятное, "
        "требующее изучения и поиска решения\n"
        "6 = Низкий + Задача — несрочная работа или доработка без чёткого срока "
        "(редкий случай)\n\n"
        "Прочитай переписку и выбери ровно одну комбинацию. Если уверенно "
        "определить нельзя — ответь «НЕ ОПРЕДЕЛЕНО».\n"
        "В конце ответа укажи только номер комбинации (или «НЕ ОПРЕДЕЛЕНО»)."
    )


def _parse_pt_combo(raw: str) -> tuple[str, str] | None:
    """(priority_id, type_id) по первому валидному номеру комбинации в ответе.

    Lenient, как _parse_env_id: терпит текст вокруг, отклоняет числа вне 1–6
    («10» — это токен «10», на «1» не распадается).
    """
    for tok in re.findall(r"\d+", raw):
        if tok in _PT_COMBOS:
            return _PT_COMBOS[tok]
    return None


_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "llama-3.3-70b-versatile"
# Fallback on a separate per-model Groq quota (survives llama-3.3 429/limits)
_GROQ_FALLBACK_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


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


async def _groq_classify(prompt: str, user_content: str, model: str = _GROQ_MODEL) -> str | None:
    if not config.groq_api_key:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
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


async def classify_environment(
    history: str, ticket_title: str = "", prior_hint: str = ""
) -> str | None:
    """Return an Окружение option_id, or None if undetermined / unknown / error.

    Keyword pre-pass first (deterministic, no API). Then llama-3.3 (fast);
    falls back to Llama 4 Scout (separate Groq quota) when llama-3.3 is
    unavailable (e.g. daily token limit / 429).
    """
    if not history.strip() and not ticket_title.strip():
        return None
    kw = _keyword_match(f"{ticket_title}\n{history}")
    if kw:
        logger.info("Env classifier: keyword pre-pass hit %s", kw)
        return kw
    parts: list[str] = []
    if ticket_title.strip():
        parts.append(f"Тема тикета: {ticket_title.strip()}")
    if prior_hint:
        parts.append(
            f"Подсказка: у этого клиента в прошлых тикетах чаще всего определялось "
            f"окружение «{prior_hint}». Используй как слабый приор, а не как ответ."
        )
    parts.append(f"Переписка:\n{history}")
    user_content = "\n\n".join(parts)
    prompt = _build_env_prompt()
    raw = await _groq_classify(prompt, user_content)
    if raw is None:
        raw = await _groq_classify(prompt, user_content, model=_GROQ_FALLBACK_MODEL)
    if raw is None:
        return None
    option_id = _parse_env_id(raw)
    if option_id is None:
        logger.info("Env classifier: no valid id in response %r", raw[:80])
    return option_id


async def classify_priority_type(
    history: str, ticket_title: str = ""
) -> tuple[str, str] | None:
    """(priority_id, type_id) по матрице комбинаций, или None если не определено.

    Один LLM-вызов на оба поля: приоритет и тип — одно решение.
    llama-3.3 (fast), фолбэк Llama 4 Scout. Keyword pre-pass не делаем:
    «блокирует/не блокирует торговлю» регулярками не различить.
    """
    if not history.strip() and not ticket_title.strip():
        return None
    parts: list[str] = []
    if ticket_title.strip():
        parts.append(f"Тема тикета: {ticket_title.strip()}")
    parts.append(f"Переписка:\n{history}")
    user_content = "\n\n".join(parts)
    prompt = _build_pt_prompt()
    raw = await _groq_classify(prompt, user_content)
    if raw is None:
        raw = await _groq_classify(prompt, user_content, model=_GROQ_FALLBACK_MODEL)
    if raw is None:
        return None
    combo = _parse_pt_combo(raw)
    if combo is None:
        logger.info("PT classifier: no valid combo in response %r", raw[:80])
    return combo


ENV_UNDETERMINED_MSG = "⚠️ Окружение не определено автоматически — выставьте вручную"


PT_UNDETERMINED_MSG = "⚠️ Приоритет и тип не определены — выставьте вручную"


@dataclass
class AutofillResult:
    """Outcome of apply_ticket_fields, for callers that want feedback.

    updated:     the HDE PUT succeeded.
    fields:      the custom_fields map that was sent (empty if not updated).
    env_id:      classified Окружение option_id, or None if undetermined.
    priority_id: выставленный приоритет, или None если не определено.
    type_id:     выставленный тип, или None если не определено.
    error:       human-readable reason when not updated, else None.
    """
    updated: bool
    fields: dict[str, str] = field(default_factory=dict)
    env_id: str | None = None
    priority_id: str | None = None
    type_id: str | None = None
    error: str | None = None


async def _company_prior_hint(record, ticket_id: str) -> str:
    """Человекочитаемый приор «Окружения» по прошлым тикетам той же компании."""
    if record is None or not record.company_name:
        return ""
    try:
        from . import db as _db
        prior_id = await _db.get_common_env_for_company(
            record.company_name, exclude_ticket_id=ticket_id
        )
    except Exception as exc:
        logger.warning("env prior lookup failed for %s: %s", ticket_id, exc)
        return ""
    if prior_id in OKRUZHENIE_OPTIONS:
        return OKRUZHENIE_OPTIONS[prior_id]
    return ""


async def apply_ticket_fields(
    bot: Bot,
    ticket_id: str,
    topic_id: int,
    history: str,
    ticket_title: str = "",
    posts: list | None = None,
) -> AutofillResult:
    """Auto-fill Классификация / Окружение / Роль after the AI summary.

    history обогащается Vision-описаниями фото (из ticket_topics) и
    Deepgram-транскриптами голосовых (по posts), плюс приор по компании.
    Never raises — any failure is logged so the summary flow is unaffected.
    Returns an AutofillResult; the auto-trigger hook ignores it.
    """
    try:
        client = HDEApiClient()
    except Exception as exc:
        logger.warning("apply_ticket_fields: no HDE client: %s", exc)
        return AutofillResult(updated=False, error="HDE API недоступен")

    from . import db as _db

    record = None
    try:
        record = await _db.get_topic(ticket_id)
    except Exception as exc:
        logger.warning("apply_ticket_fields: topic read failed for %s: %s", ticket_id, exc)

    enriched = history
    if record is not None and record.photo_descriptions:
        enriched += f"\n[Описание фото из тикета: {record.photo_descriptions}]"
    if posts:
        try:
            from .ai_summary import _transcribe_audio_posts
            async with aiohttp.ClientSession() as session:
                for t in await _transcribe_audio_posts(posts, session):
                    enriched += f"\n[Голосовое сообщение клиента: {t}]"
        except Exception as exc:
            logger.warning("apply_ticket_fields: transcription failed for %s: %s", ticket_id, exc)

    prior_hint = await _company_prior_hint(record, ticket_id)

    fields: dict[str, str] = {FIELD_KLASSIFIKACIYA: KLASSIFIKACIYA_OBORUDOVANIE}

    # Роль: set only if currently empty (id == 0). None = read error → skip.
    try:
        current_rol = await client.get_ticket_field_value(ticket_id, int(FIELD_ROL))
    except Exception as exc:
        logger.warning("apply_ticket_fields: role read failed for %s: %s", ticket_id, exc)
        current_rol = None
    if current_rol == 0:
        fields[FIELD_ROL] = ROL_NE_VAZHNO

    # Окружение: keyword pre-pass + LLM classify; overwrite always when determined.
    env_id = await classify_environment(enriched, ticket_title, prior_hint)
    if env_id:
        fields[FIELD_OKRUZHENIE] = env_id

    # Приоритет/Тип: однократно, при создании тикета; ручные правки
    # оператора потом не перезаписываются (повторных попыток нет).
    pt = await classify_priority_type(enriched, ticket_title)
    priority_id, type_id = pt if pt else (None, None)

    try:
        await client.update_ticket_fields(
            ticket_id, fields, priority_id=priority_id, type_id=type_id
        )
        logger.info("apply_ticket_fields: ticket %s updated %s", ticket_id, fields)
    except Exception as exc:
        logger.warning("apply_ticket_fields: update failed for %s: %s", ticket_id, exc)
        return AutofillResult(
            updated=False, fields=fields, env_id=env_id,
            priority_id=priority_id, type_id=type_id, error="ошибка записи в HDE"
        )

    # Запоминаем исход: '' = «не определено» (триггер для реклассификации),
    # цифры = выбранная опция (источник приора и сверки с оператором).
    try:
        await _db.update_topic(ticket_id, env_option_id=env_id or "")
    except Exception as exc:
        logger.warning("apply_ticket_fields: env store failed for %s: %s", ticket_id, exc)

    try:
        await _db.update_topic(
            ticket_id,
            priority_option_id=priority_id if priority_id is not None else "",
            type_option_id=type_id if type_id is not None else "",
        )
    except Exception as exc:
        logger.warning("apply_ticket_fields: pt store failed for %s: %s", ticket_id, exc)

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

    if pt is None:
        try:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=PT_UNDETERMINED_MSG,
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning(
                "apply_ticket_fields: pt warn-message failed for topic %s: %s",
                topic_id, exc,
            )

    return AutofillResult(updated=True, fields=fields, env_id=env_id,
                          priority_id=priority_id, type_id=type_id)


async def retry_env_classification(bot: Bot, ticket_id: str, topic_id: int) -> None:
    """Повторная классификация «Окружения» после нового сообщения клиента.

    Вызывается только когда прошлая попытка дала «не определено»
    (env_option_id == ''). Тихая: при неудаче ничего не пишет в топик —
    предупреждение уже было показано. Голосовые не транскрибируются повторно
    (экономия Deepgram; поздние сообщения почти всегда текстовые).
    Never raises.
    """
    try:
        from . import db as _db
        from .ai_summary import _build_history_text

        client = HDEApiClient()
        info = await client.get_ticket_info(ticket_id)
        posts = await client.get_ticket_posts(ticket_id)
        try:
            comments = await client.get_ticket_comments(ticket_id)
        except Exception:
            comments = []
        all_posts = sorted(posts + comments, key=lambda p: p.date_created)
        history = _build_history_text(all_posts, info)

        record = await _db.get_topic(ticket_id)
        ticket_title = record.ticket_name if record is not None else ""
        if record is not None and record.photo_descriptions:
            history += f"\n[Описание фото из тикета: {record.photo_descriptions}]"
        prior_hint = await _company_prior_hint(record, ticket_id)

        env_id = await classify_environment(history, ticket_title, prior_hint)
        if not env_id:
            return

        await client.update_ticket_fields(ticket_id, {FIELD_OKRUZHENIE: env_id})
        await _db.update_topic(ticket_id, env_option_id=env_id)
        logger.info("retry_env_classification: ticket %s env=%s", ticket_id, env_id)
        try:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=f"🧩 Окружение определено по новым сообщениям: {OKRUZHENIE_OPTIONS[env_id]}",
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning("retry_env_classification: notify failed for %s: %s", ticket_id, exc)
    except Exception as exc:
        logger.warning("retry_env_classification failed for %s: %s", ticket_id, exc)


ENV_CORRECTIONS_PATH = "data/env_corrections.jsonl"


async def log_env_outcome(ticket_id: str, predicted: str | None) -> None:
    """При закрытии тикета фиксирует наш прогноз vs финальное значение поля.

    Оператор мог поправить «Окружение» вручную — JSONL даёт метрику точности
    и материал для улучшения промпта. Never raises.
    """
    try:
        client = HDEApiClient()
        final = await client.get_ticket_field_value(ticket_id, int(FIELD_OKRUZHENIE))
        if final is None:
            return
        final_str = str(final) if final else None
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticket_id": ticket_id,
            "predicted": predicted,
            "final": final_str,
            "match": bool(predicted) and final_str == predicted,
        }
        os.makedirs(os.path.dirname(ENV_CORRECTIONS_PATH) or ".", exist_ok=True)
        with open(ENV_CORRECTIONS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("log_env_outcome failed for %s: %s", ticket_id, exc)
