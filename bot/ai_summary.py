"""Generate ticket summary via Groq API (text model + multimodal fallback)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from . import metrics
from .config import config
from .hde_api import shared_session
from .llm_semaphore import LLM_SEMAPHORE
from .voice_profile import load_voice_examples

if TYPE_CHECKING:
    from .hde_api import HDEPost, HDETicketInfo

logger = logging.getLogger(__name__)

def _load_few_shot_examples() -> list[dict]:
    path = Path(__file__).parent / "prompts" / "few_shot_examples.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("examples", [])
    except Exception:
        return []


def _strip_reasoning(text: str) -> str:
    import re as _re
    text = _re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=_re.DOTALL)
    # reasoning-модели (qwen3, gpt-oss) выдают <think>…</think> — тоже вырезаем
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)
    return text.strip()


_FEW_SHOT_EXAMPLES: list[dict] = _load_few_shot_examples()
_VOICE_EXAMPLES: list[str] = load_voice_examples()

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

_AUDIO_TYPES = {"mp3", "ogg", "wav", "m4a", "opus", "aac", "flac", "oga"}
_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

_IMAGE_TYPES = {"jpg", "jpeg", "png", "gif", "webp"}
_IMAGE_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
_MAX_IMAGES = 3          # max images per vision request
_MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB per image

# --- Бюджет запроса под TPM ------------------------------------------------
# Провайдер считает против лимита prompt + max_tokens, а лимит на прод-модели
# 8000 TPM. До этих ограничений тикет с длинной историей и большими примерами из
# базы получал 413 «Request too large» и оставался ВООБЩЕ без суммарки (50 таких
# ошибок в прод-логе за сутки, плюс 123 отказа 429).
#
# Пределы выбраны по замерам: статика промпта (инструкции + few-shot + голос) уже
# ~9.8k символов, один knowledge_item в базе доходит до 20k символов, а RAG даёт
# до трёх примеров — без обрезки только они давали до 60k символов.
_RAG_EXCERPT_LIMIT = 600      # столько же, сколько у агента (agent/context.py)
_MAX_HISTORY_CHARS = 3000
_MAX_PROMPT_CHARS = 16000     # ≈6k токенов; вместе с выводом влезает в 8000 TPM

_EQUIPMENT_PATTERNS = [
    (r'\bатол\b|atol|\bфр\b', 'АТОЛ'),
    (r'\bэвотор\b|evotor', 'Эвотор'),
    (r'\bштрих\b|shtrih|shtrikh', 'Штрих-М'),
    (r'\bviki\b|вики', 'Viki'),
    (r'\bсбербанк|\bсбер|sber', 'Эквайринг Сбер'),
    (r'\bвтб\b|vtb', 'Эквайринг ВТБ'),
    (r'\bтинькофф\b|tinkoff|тиньков', 'Эквайринг Тинькофф'),
    (r'\bптк\b|ptkf', 'ПТК'),
]


async def call_groq_text(
    prompt: str,
    *,
    system: str | None = None,
    model: str = "",
    max_tokens: int = 300,
    temperature: float = 0.1,
    timeout_seconds: float = 20,
    reasoning_effort: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str | None:
    """One-shot Groq chat completion via the shared session and LLM semaphore.

    The single entry point for ad-hoc text calls outside the summary pipeline.
    Returns the reply text, or None on any failure (missing key, non-200,
    network error) — callers treat LLM output as optional.

    model пусто → модель суммарки из env (жёсткий llama-3.3 Groq отключил).
    reasoning_effort (напр. "none") прокидывается для reasoning-моделей (qwen3),
    чтобы отключить <think> и не жечь токены; None → значение из config.
    base_url/api_key — другой OpenAI-совместимый провайдер вместо Groq (агент v2);
    reasoning_effort ему передавайте "" — это параметр Groq.
    """
    # ключ Groq чужому провайдеру не отдаём, даже если свой ключ забыли прописать
    key = api_key if base_url else config.groq_api_key
    if not key:
        return None
    url = f"{base_url.rstrip('/')}/chat/completions" if base_url else _GROQ_URL
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {
        "model": model or config.groq_summary_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if reasoning_effort is None:
        reasoning_effort = config.groq_reasoning_effort
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    started = time.monotonic()
    try:
        async with LLM_SEMAPHORE, shared_session() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Groq text call error %s: %s", resp.status, body[:200])
                    metrics.inc("llm_failures")
                    return None
                data = await resp.json()
        metrics.inc("llm_calls")
        metrics.observe_llm_latency(time.monotonic() - started)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("Groq text call failed: %s", exc)
        metrics.inc("llm_failures")
        return None


async def call_groq_json(
    system: str,
    user: str,
    *,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 600,
    reasoning_effort: str = "",
) -> str | None:
    """Публичный JSON-вызов Groq для runtime-агента (self-check и т.п.)."""
    if not config.groq_api_key:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    headers = {"Authorization": f"Bearer {config.groq_api_key}"}
    try:
        async with LLM_SEMAPHORE, shared_session() as session:
            async with session.post(
                _GROQ_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning("call_groq_json: HTTP %s", resp.status)
                    return None
                data = await resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("call_groq_json failed: %s", exc)
        return None


async def _call_groq_for_summary(system_text: str, history: str, ticket_id: str) -> str | None:
    """Primary: text-only summary via the Groq model from env."""
    if not config.groq_api_key:
        logger.info("Groq fallback skipped: GROQ_API_KEY not set")
        return None
    started = time.monotonic()
    summary_payload: dict = {
        "model": config.groq_summary_model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": f"Переписка:\n{history}"},
        ],
        "max_tokens": config.groq_summary_max_tokens,
        "temperature": 0.3,
    }
    if config.groq_reasoning_effort:
        summary_payload["reasoning_effort"] = config.groq_reasoning_effort
    try:
        async with LLM_SEMAPHORE, shared_session() as session:
            async with session.post(
                _GROQ_URL,
                json=summary_payload,
                headers={"Authorization": f"Bearer {config.groq_api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Groq API error %s for ticket %s: %s", resp.status, ticket_id, body[:200])
                    metrics.inc("llm_failures")
                    return None
                data = await resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        metrics.inc("llm_calls")
        metrics.observe_llm_latency(time.monotonic() - started)
        logger.info("Groq fallback succeeded for ticket %s", ticket_id)
        return text
    except Exception as exc:
        logger.warning("Groq request failed for ticket %s: %s", ticket_id, exc)
        metrics.inc("llm_failures")
        return None


async def _call_groq_vision_for_summary(
    system_text: str,
    history: str,
    image_parts: list[dict],
    ticket_id: str,
    session: aiohttp.ClientSession,
) -> str | None:
    """Fallback: summary via the Groq multimodal model (images + text).

    Retries on 429/503 — Groq limits are per-minute, short waits suffice.
    """
    if not config.groq_api_key:
        return None
    content: list[dict] = [{"type": "text", "text": f"Переписка:\n{history}"}] + image_parts
    payload = {
        "model": config.groq_vision_model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": content},
        ],
        "max_tokens": config.groq_summary_max_tokens,
        "temperature": 0.3,
    }
    if config.groq_reasoning_effort:
        payload["reasoning_effort"] = config.groq_reasoning_effort
    for attempt in range(3):
        try:
            async with LLM_SEMAPHORE, session.post(
                _GROQ_URL,
                json=payload,
                headers={"Authorization": f"Bearer {config.groq_api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                status = resp.status
                if status in (429, 503):
                    await resp.read()  # drain connection
                elif status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Groq vision API error %s for ticket %s: %s",
                        status, ticket_id, body[:200],
                    )
                    return None  # non-retryable
                else:
                    data = await resp.json()
                    text = (data["choices"][0]["message"]["content"] or "").strip()
                    logger.info("Groq vision fallback succeeded for ticket %s", ticket_id)
                    return text or None
            if attempt < 2:
                wait = 20 if status == 429 else 15
                logger.warning(
                    "Groq vision %s for ticket %s, retry in %ss (attempt %d/3)",
                    status, ticket_id, wait, attempt + 1,
                )
                await asyncio.sleep(wait)
                continue
            logger.warning("Groq vision %s after 3 attempts for ticket %s", status, ticket_id)
            return None
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("Unexpected Groq vision response for ticket %s: %s", ticket_id, exc)
            return None
        except Exception as exc:
            if attempt < 2:
                logger.warning(
                    "Groq vision request error attempt %d for ticket %s: %s",
                    attempt + 1, ticket_id, exc,
                )
                await asyncio.sleep(5)
                continue
            logger.warning("Groq vision failed after 3 attempts for ticket %s: %s", ticket_id, exc)
            return None
    return None


def _detect_equipment(title: str, history: str) -> str | None:
    """Return equipment brand from ticket title + start of history, or None."""
    import re
    text = (title + " " + history[:300]).lower()
    for pattern, brand in _EQUIPMENT_PATTERNS:
        if re.search(pattern, text):
            return brand
    return None


_FORMAT_INSTRUCTIONS = (
    "Если выше есть «Типовые шаги решения» или «Справочная статья» — используй их как основной источник\n"
    "для Памятки и Клиенту. Не повторяй их дословно, но опирайся на конкретные шаги, пути, команды.\n\n"
    "Перед ответом заполни блок рассуждения (скрыт от пользователя):\n"
    "<reasoning>\n"
    "1. Что именно сломано технически — конкретная причина, не симптом?\n"
    "2. Какая модель/ПО затронута?\n"
    "3. На каком этапе тикет: первичная диагностика, удалённая работа, ожидание действия клиента, выезд?\n"
    "4. Что уже пробовали? Есть ли фото/скриншоты в переписке — что на них видно?\n"
    "5. Применима ли информация из БЗ/шагов решения к этому конкретному тикету?\n"
    "6. Какой следующий шаг для меня как инженера: удалённый доступ, звонок вендору, выезд, дождаться от клиента?\n"
    "</reasoning>\n\n"
    "Формат ответа — ровно три метки, каждая с новой строки:\n"
    "Суть: <одно предложение, 8–18 слов, что именно сломано и у какого ПО/оборудования>\n"
    "Клиенту: <одно предложение, императив, максимум 20 слов — либо один уточняющий вопрос>\n"
    "Памятка: <конкретика для специалиста — либо тире, если сказать нечего>\n\n"
    "═══════════════ «Суть» — ЖЁСТКИЕ ПРАВИЛА ═══════════════\n"
    "1. Одно предложение, 8–18 слов. Это диагноз, а не категория.\n"
    "2. Обязательно — что именно не работает + модель/ПО (Эвотор, Атол 30Ф, Posiflora, RuDesktop, Сбер-эквайринг).\n"
    "3. Без воды: не писать «клиент обратился с проблемой», «требуется диагностика», «обращение связано с».\n"
    "4. Формулируй как инженер коллеге, а не как отчёт.\n"
    "5. Для настройки/установки (не поломки): «Первичная настройка X на Y — [что именно нужно сделать].»\n\n"
    "Хорошие примеры «Суть»:\n"
    "- «Эвотор не видит ККТ Атол 30Ф после обновления прошивки.»\n"
    "- «Posiflora не проводит оплату через терминал Сбера, ошибка связи.»\n"
    "- «X-printer 365B не печатает чеки после смены USB-порта.»\n"
    "- «Не активируется лицензия Posiflora, код активации не принимается.»\n"
    "- «Первичная настройка принтера X-printer 365B для заказов в Posiflora.»\n"
    "- «Перенос Posiflora на новый ПК — касса Атол не настроена после переноса.»\n\n"
    "Плохие примеры «Суть» (так НЕ пиши):\n"
    "- «Суть: диагностика» — это категория, а не диагноз.\n"
    "- «Суть: подключение» — слишком общо, без ПО и симптома.\n"
    "- «Суть: клиент обратился с проблемой подключения оборудования» — вода.\n\n"
    "═══════════════ «Клиенту» — ЖЁСТКИЕ ПРАВИЛА ═══════════════\n"
    "1. Длина: одно предложение, ≤20 слов. Никаких абзацев.\n"
    "2. Императив: Подключите, Пришлите, Откройте, Проверьте, Перезагрузите, Уточните. Без «Вам нужно», «необходимо выполнить».\n"
    "3. Никаких приветствий («Здравствуйте»), представлений («Специалист Дина»), прощаний, «в ближайшее время», «мы свяжемся».\n"
    "4. Слово «пожалуйста» — не более одного раза на сообщение. Лучше без него.\n"
    "5. Обязательно должен быть хотя бы один конкретный элемент: модель (Атол 30Ф, Эвотор, X-printer 365B), ПО (AnyDesk / RuDesktop — не смешивать!), путь (Настройки → Банковский терминал), или что прислать (ID и пароль, фото экрана, номер телефона).\n"
    "6. Запрещено утверждать то, что ещё не произошло: «Приложение активировано», «Сейчас всё работает», «Подключаю кассу». Если действие не выполнено — не пиши, что выполнено.\n"
    "7. ПРАВИЛО ВЫБОРА:\n"
    "   - Нет информации для диагностики → ОДИН прямой вопрос по сути, ≤15 слов, без обёрток. «Каким способом подключить Атол 30Ф — USB или Wi-Fi?», НЕ «Необходимо уточнить предпочтительный способ подключения, пожалуйста, уточните…».\n"
    "   - Есть чёткий следующий шаг → конкретная инструкция (что сделать, с каким ПО/оборудованием).\n"
    "   - Шаг выполнен / нужно подтвердить результат → краткое уточнение (получилось / нет).\n"
    "8. Запретные фразы (по одной видна — переписывай): «Для дальнейшей диагностики», «необходимо выполнить следующие шаги», «позволит нашему специалисту», «Надеюсь это поможет», «Если есть вопросы», «Спасибо за обращение», «уточнить детали и согласовать», «чтобы мы могли корректно помочь», «предоставьте информацию», «необходимо уточнить», «дайте знать», «требуется ли».\n\n"
    "Хорошие примеры «Клиенту» (из реальных отправленных операторами):\n"
    "- «Откройте RuDesktop, пришлите код для подключения и подготовьте компьютер для удалённого доступа.»\n"
    "- «Примите запрос на подключение в AnyDesk, подключите кассу Атол к компьютеру по USB и включите в сеть.»\n"
    "- «Проверьте IP-адрес и порт Эвотора в настройках Posiflora, затем попробуйте провести оплату.»\n"
    "- «Перезагрузите Wi-Fi роутер и кассу Эвотор кнопкой питания.»\n"
    "- «Уточните, какие ошибки выводит касса на экране — пришлите фото.»\n"
    "- «Каким способом подключить Атол 30Ф — по USB или Wi-Fi?»\n\n"
    "Плохие примеры (так НЕ пиши):\n"
    "- «Пожалуйста, уточните, требуется ли проверка настроек принтера, и предоставьте информацию о модели, чтобы мы могли корректно помочь.» → обёртки, косвенный вопрос, вода. Надо: «Какая модель принтера и как подключён — USB или сеть?»\n"
    "- «Здравствуйте, Олеся! Специалист тех. поддержки Дина. Вижу ваш RuDesktop ID 400180391. Для дальнейшей диагностики...» → слишком длинно, представление, воды.\n"
    "- «Для подключения кассы необходимо выполнить следующие шаги:» → оборвано, штамп, нет конкретики.\n"
    "- «Нам нужно удалённо подключиться, чтобы восстановить настройки, пожалуйста, подготовьте компьютер.» → дважды вежливость, нет конкретного ПО.\n\n"
    "═══════════════ «Памятка» — ЖЁСТКИЕ ПРАВИЛА ═══════════════\n"
    "Памятка пишется ДЛЯ ОПЕРАТОРА, а не для клиента. Это шпаргалка, которая экономит ему время.\n\n"
    "Есть 2 и более конкретных слота — пиши. Есть 0–1 — ставь прочерк «—»:\n"
    "  (1) конкретное устройство/ПО с моделью (Атол 30Ф, Эвотор 5, X-printer 365B, RuDesktop 1.8)\n"
    "  (2) путь в меню или команда (Боковое меню → Настройки → Фискальное устройство)\n"
    "  (3) ссылка на статью БЗ из контекста (clck.ru/..., posiflora.teamly.ru/...)\n"
    "  (4) куда звонить или выезжать: горячая линия вендора/банка (Сбер 0321, Атол ТП, Эвотор ТП), или «нужен выезд»\n"
    "  (5) «если не помогло» с именованным симптомом (если касса не видит ККТ — проверить USB-кабель и порт)\n\n"
    "Разделитель между слотами — ` • ` (пробел-точка-пробел).\n\n"
    "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ памятки без конкретики. В частности — вот эти штампы:\n"
    "- «диагностика → шаги → эскалация»\n"
    "- «Подключиться → Проверить → Уточнить»\n"
    "- «категория → что проверить → если не помогло: эскалация»\n"
    "- любая памятка, где нет ни одного имени собственного (модели, ПО, меню, ссылки)\n\n"
    "Если полезной специалисту информации нет — честно пиши:\n"
    "Памятка: —\n\n"
    "Хорошие примеры «Памятка»:\n"
    "- «RuDesktop на стороне клиента • прислать ID+пароль • если зависает — переустановить с clck.ru/xxx • если не помогло: позвонить в Атол ТП.»\n"
    "- «Эвотор 5 + Posiflora • Настройки → Банковский терминал → IP/порт • если терминал не отвечает — Сбер 0321 (не Сбер Бизнес).»\n"
    "- «АТОЛ 30Ф по USB • драйверы https://clck.ru/yyy • если не определяется — проверить USB-кабель и сменить порт.»\n\n"
    "Плохие примеры «Памятка»:\n"
    "- «диагностика → шаги → эскалация» — штамп без содержания.\n"
    "- «подключение → проверка → решение» — вода.\n\n"
    "Без markdown. Без лишнего текста до или после трёх меток."
)

_active_prompt_loaded: bool = False
_active_format_instructions: str | None = None

try:
    from . import db  # noqa: E402  — available at runtime, may be absent in tests
except ImportError:
    db = None  # type: ignore[assignment]


async def get_active_format_instructions() -> str:
    """Return active FORMAT_INSTRUCTIONS from DB (lazily loaded, cached in memory).

    Falls back to built-in _FORMAT_INSTRUCTIONS if nothing in DB.
    Call invalidate_prompt_cache() after applying a new version.
    """
    global _active_prompt_loaded, _active_format_instructions
    if not _active_prompt_loaded:
        try:
            _active_format_instructions = await db.get_active_prompt()  # type: ignore[union-attr]
        except Exception:
            _active_format_instructions = None
        _active_prompt_loaded = True
    return _active_format_instructions or _FORMAT_INSTRUCTIONS


def prompt_version_tag() -> str:
    """Метка версии промпта для трассировки ai_suggestions.
    Согласована между run_agent и register_feedback_pending (общий idempotency_key)."""
    if config.agent_voice_v2_enabled:
        return "voice-v2"
    if _active_prompt_loaded and _active_format_instructions is not None:
        return "db-active"
    return "legacy"


_routing_cache: tuple[float, list] | None = None


def _load_routing_rules() -> list:
    """Карта ответственности с кешем по mtime файла.

    Читается на каждом черновике, поэтому файл не парсится заново без нужды;
    правка карты подхватывается без перезапуска бота.
    """
    global _routing_cache
    from .agent.routing_map import DEFAULT_PATH, load_routing_map

    try:
        mtime = DEFAULT_PATH.stat().st_mtime
    except OSError:
        _routing_cache = None
        return []
    if _routing_cache is not None and _routing_cache[0] == mtime:
        return _routing_cache[1]
    rules = load_routing_map()
    _routing_cache = (mtime, rules)
    return rules


def _build_system_prompt(
    ticket_title: str,
    rag_examples: list[str] | None = None,
    wiki_context: str | None = None,
    equipment: str | None = None,
    solution_steps: str | None = None,
    format_instructions: str | None = None,
) -> str:
    instr = format_instructions or _FORMAT_INSTRUCTIONS
    base = (
        "Ты — помощник инженера по кассовому оборудованию (2-я линия поддержки).\n"
        "Специализация: АТОЛ, Эвотор, Штрих-М, Viki, фискальные регистраторы, ОФД/ФН,\n"
        "сетевые подключения, эквайринг (Сбер, ВТБ, Т-Банк).\n\n"
        "Тикет передан с 1-й линии — базовую диагностику уже провели.\n"
        "Оператор — инженер по оборудованию. НЕ предлагай эскалацию «на инженера» — это и есть он.\n"
        "Если проблема требует выезда или звонка в банк/вендор — указывай именно это.\n\n"
    )
    if ticket_title:
        base += f"Тема обращения: «{ticket_title}»\n\n"
    if equipment:
        base += f"Оборудование в тикете: {equipment}\n\n"
    if solution_steps:
        base += (
            "Типовые шаги решения для этого типа проблемы:\n"
            f"{solution_steps}\n\n"
            "---\n\n"
        )
    if wiki_context:
        base += (
            "Справочная статья из базы знаний:\n\n"
            f"{wiki_context}\n\n"
            "---\n\n"
        )
    # Карта ответственности идёт ДО примеров намеренно: few-shot сильнее
    # инструкции (правило, которому противоречат образцы, модель нарушит), и
    # правило уровня политики, зажатое после примеров, проигрывает им.
    # Отсутствие карты — рабочий режим: блок пустой, промпт как был.
    try:
        from .agent.routing_map import format_routing_block  # noqa: PLC0415
        routing_block = format_routing_block(_load_routing_rules())
    except Exception as exc:
        logger.warning("routing map block skipped: %s", exc)
        routing_block = ""
    if routing_block:
        base += f"{routing_block}\n\n---\n\n"
    if _FEW_SHOT_EXAMPLES:
        shots = []
        for ex in _FEW_SHOT_EXAMPLES:
            shots.append(
                f"Проблема: {ex.get('problem', '')}\n"
                f"Суть: {ex.get('suit', '')}\n"
                f"Клиенту: {ex.get('client', '')}\n"
                f"Памятка: {ex.get('pamyatka', '')}"
            )
        base += (
            "Эталонные примеры (из принятых ответов операторов):\n\n"
            + "\n\n---\n\n".join(shots)
            + "\n\n---\n\n"
        )
    if _VOICE_EXAMPLES:
        base += (
            "Реальные ответы оператора клиентам. Пиши поле «Клиенту» в этом стиле —\n"
            "та же длина, тот же тон, без шаблонной вежливости:\n\n"
            + "\n".join(f"— {ex}" for ex in _VOICE_EXAMPLES)
            + "\n\n---\n\n"
        )
    if rag_examples:
        examples_text = "\n\n---\n\n".join(rag_examples)
        base += (
            "Примеры решений из практики:\n\n"
            f"{examples_text}\n\n"
            "---\n\n"
            "Теперь обработай новый тикет:\n\n"
        )
    return base + instr


def _build_history_text(posts: "list[HDEPost]", info: "HDETicketInfo") -> str:
    """Convert posts to plain text for the prompt.

    Internal comments (is_comment=True) are included as "Коллега" so the AI
    has full context from first-line support notes.
    """
    import re
    from html import unescape

    lines: list[str] = []
    for post in posts:
        if post.is_comment:
            role = "Коллега"
        elif post.user_id == info.client_id:
            role = "Клиент"
        else:
            role = "Сотрудник"
        text = re.sub(r"<[^>]+>", "", post.text)
        text = unescape(text).strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _budget_history(posts: "list[HDEPost]", info: "HDETicketInfo") -> str:
    """История под бюджет символов, целыми сообщениями.

    Тот же приём, что у агента (`agent.context.build_history_budgeted`): первое
    сообщение клиента плюс хвост целыми репликами. Резать посередине реплики
    нельзя — модель достраивает обрубок и выдумывает.
    """
    from .agent.context import build_history_budgeted  # noqa: PLC0415 — цикл импорта

    return build_history_budgeted(
        posts, info, budget=_MAX_HISTORY_CHARS, _history_fn=_build_history_text
    )


def _fit_prompt(
    system_text: str, history: str, assemble, ticket_id: str = ""
) -> tuple[str, str]:
    """Уложить запрос в бюджет: сначала без примеров из базы, потом обрезкой истории.

    Порядок деградации не произвольный. Примеры RAG — самое объёмное и самое
    заменимое: без них модель отвечает хуже, но отвечает. История — то, из чего
    вообще делается диагноз, поэтому её режем последней и с головы, оставляя
    свежие реплики. Полный отказ (413 и никакой суммарки) хуже обоих вариантов.
    """
    if len(system_text) + len(history) <= _MAX_PROMPT_CHARS:
        return system_text, history

    without_rag = assemble(None)
    if len(without_rag) + len(history) <= _MAX_PROMPT_CHARS:
        logger.info(
            "Prompt budget for ticket %s: dropped RAG examples (%d → %d chars)",
            ticket_id, len(system_text) + len(history), len(without_rag) + len(history),
        )
        return without_rag, history

    room = max(_MAX_PROMPT_CHARS - len(without_rag), 500)
    trimmed = history[-room:]
    logger.warning(
        "Prompt budget for ticket %s: history trimmed %d → %d chars after dropping RAG",
        ticket_id, len(history), len(trimmed),
    )
    return without_rag, f"[...начало переписки пропущено...]\n{trimmed}"


def _write_generation_log(entry: dict) -> None:
    os.makedirs("data", exist_ok=True)
    with open("data/ai_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def _log_generation(
    ticket_id: str,
    ticket_title: str,
    history: str,
    generated: str,
) -> None:
    """Append a JSONL entry to data/ai_log.jsonl for future few-shot curation.

    File I/O runs in a worker thread so the event loop is never blocked.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticket_id": ticket_id,
        "ticket_title": ticket_title,
        "history": history,
        "generated": generated,
        "operator_reply": None,
    }
    try:
        await asyncio.to_thread(_write_generation_log, entry)
    except OSError as exc:
        logger.warning("Could not write ai_log.jsonl: %s", exc)


async def _transcribe_audio_posts(
    posts: "list[HDEPost]",
    session: aiohttp.ClientSession,
) -> list[str]:
    """Transcribe audio attachments via Deepgram. Returns list of transcript strings."""
    if not config.deepgram_api_key:
        return []
    auth = aiohttp.BasicAuth(config.hde_api_email, config.hde_api_key)
    candidates: list[tuple[str, str, str]] = []  # (url, data_type, name)
    for post in posts:
        for file_info in (post.files or []):
            data_type = (file_info.get("data_type") or "").lower().lstrip(".")
            if data_type not in _AUDIO_TYPES:
                continue
            url = file_info.get("url", "")
            if not url:
                continue
            candidates.append((url, data_type, file_info.get("name") or ""))
    if not candidates:
        return []

    # Download + transcribe each file as one task, max 3 in flight;
    # transcripts keep conversation order.
    sem = asyncio.Semaphore(3)

    async def _transcribe(url: str, data_type: str, name: str) -> str | None:
        async with sem:
            try:
                async with session.get(
                    url, auth=auth, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Audio download failed %s: HTTP %s", url, resp.status)
                        return None
                    audio_data = await resp.read()
            except Exception as exc:
                logger.warning("Audio download error %s: %s", url, exc)
                return None
            mime_type = f"audio/{data_type}"
            try:
                async with session.post(
                    _DEEPGRAM_URL,
                    data=audio_data,
                    headers={
                        "Authorization": f"Token {config.deepgram_api_key}",
                        "Content-Type": mime_type,
                    },
                    params={"language": "ru", "model": "nova-2", "smart_format": "true"},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("Deepgram error %s: %s", resp.status, body[:200])
                        return None
                    result = await resp.json()
                    transcript = (
                        result.get("results", {})
                        .get("channels", [{}])[0]
                        .get("alternatives", [{}])[0]
                        .get("transcript", "")
                        .strip()
                    )
            except Exception as exc:
                logger.warning("Deepgram transcription failed for %s: %s", url, exc)
                return None
            if transcript:
                logger.info(
                    "Deepgram transcribed audio %s: %r...", name, transcript[:80]
                )
                return transcript
            return None

    results = await asyncio.gather(*(_transcribe(u, dt, n) for u, dt, n in candidates))
    return [t for t in results if t]


async def _collect_image_parts(
    posts: "list[HDEPost]",
    session: aiohttp.ClientSession,
) -> list[dict]:
    """Download images from post attachments, return OpenAI-style image_url parts.

    Downloads run concurrently (max 4 at a time); parts keep conversation
    order. Two extra candidates beyond _MAX_IMAGES cover failed downloads.
    """
    auth = aiohttp.BasicAuth(config.hde_api_email, config.hde_api_key)
    candidates: list[tuple[str, str, str]] = []  # (url, data_type, name)
    for post in posts:
        for file_info in (post.files or []):
            data_type = (file_info.get("data_type") or "").lower().lstrip(".")
            if data_type not in _IMAGE_TYPES:
                continue
            url = file_info.get("url", "")
            if not url:
                continue
            candidates.append((url, data_type, file_info.get("name") or ""))
    candidates = candidates[: _MAX_IMAGES + 2]
    if not candidates:
        return []

    sem = asyncio.Semaphore(4)

    async def _download(url: str, data_type: str, name: str) -> dict | None:
        async with sem:
            try:
                async with session.get(
                    url,
                    auth=auth,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Image download failed %s: HTTP %s", url, resp.status)
                        return None
                    raw = await resp.read()
            except Exception as exc:
                logger.debug("Image download error %s: %s", url, exc)
                return None
        if len(raw) > _MAX_IMAGE_BYTES:
            logger.debug("Image too large (%d bytes), skipping %s", len(raw), url)
            return None
        mime = _IMAGE_MIME.get(data_type, "image/jpeg")
        logger.debug("Added image %s (%d bytes) to vision request", name, len(raw))
        b64 = base64.b64encode(raw).decode()
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }

    results = await asyncio.gather(*(_download(u, dt, n) for u, dt, n in candidates))
    return [p for p in results if p is not None][:_MAX_IMAGES]


async def generate_ticket_summary(
    posts: "list[HDEPost]",
    info: "HDETicketInfo",
    ticket_title: str = "",
    ticket_id: str = "",
    company_id: str = "",
) -> tuple[str, str, str, int] | None:
    """Return (suit_line, client_line, memo_line, confidence_pct) or None if disabled/failed."""
    if not config.groq_api_key:
        logger.info("AI summary skipped: GROQ_API_KEY not set")
        return None
    if not posts:
        logger.info("AI summary skipped: no posts for ticket %s", ticket_id)
        return None

    # Check persistent toggle
    from . import db as _db
    enabled = await _db.get_setting("ai_summary_enabled", "1")
    if enabled != "1":
        logger.info("AI summary skipped: disabled (ai_summary_enabled=%s)", enabled)
        return None

    history = _budget_history(posts, info)
    if not history.strip():
        logger.info("AI summary skipped: empty history for ticket %s", ticket_id)
        return None

    # Equipment detection
    equipment = _detect_equipment(ticket_title, history)

    # RAG: find similar examples from knowledge base
    rag_examples: list[str] = []
    confidence_pct: int = 0
    try:
        from .knowledge.indexer import get_rag_context
        rag_examples, confidence_pct = await get_rag_context(
            ticket_title, history, company_id=company_id
        )
    except Exception as exc:
        logger.warning("RAG context retrieval failed: %s", exc)

    # Solution pattern lookup
    solution_steps: str | None = None
    try:
        from . import db as _db2
        pattern = await _db2.find_solution_pattern(equipment, ticket_title)
        if pattern:
            solution_steps = pattern["steps"]
            await _db2.increment_pattern_use(pattern["id"])
            logger.debug("Pattern hit for ticket %s: %r", ticket_id, solution_steps[:60])
    except Exception as exc:
        logger.warning("Solution pattern lookup failed: %s", exc)

    # Wiki: find relevant article for this topic
    wiki_ctx: str | None = None
    try:
        from .wiki.searcher import get_wiki_context
        wiki_ctx = await get_wiki_context(ticket_title)
        if wiki_ctx:
            logger.info("Wiki context found for ticket %s (%d chars)", ticket_id, len(wiki_ctx))
    except Exception as exc:
        logger.warning("Wiki context retrieval failed: %s", exc)

    # Внутренняя БЗ (Teamly) — тот же слот промпта, что и wiki: одна справочная
    # статья, а не пример ответа. Со ссылкой, чтобы оператор мог открыть источник.
    if not wiki_ctx:
        try:
            from .knowledge.indexer import get_kb_context
            kb = await get_kb_context(ticket_title, history)
            if kb:
                kb_text, kb_url = kb
                wiki_ctx = f"{kb_text}\n\nИсточник: {kb_url}" if kb_url else kb_text
                logger.info(
                    "KB article for ticket %s: %s (%d chars)",
                    ticket_id, kb_url or "no url", len(kb_text),
                )
        except Exception as exc:
            logger.warning("KB context retrieval failed: %s", exc)

    format_instructions = await get_active_format_instructions()

    def _assemble(examples: list[str] | None) -> str:
        return _build_system_prompt(
            ticket_title,
            examples,
            wiki_ctx,
            equipment=equipment,
            solution_steps=solution_steps,
            format_instructions=format_instructions,
        )

    # Примеры из базы обрезаются: один knowledge_item доходит до 20k символов, а
    # их до трёх — без обрезки промпт не влезал в TPM (у агента тот же лимит).
    capped_examples = [e[:_RAG_EXCERPT_LIMIT] for e in rag_examples] or None
    system_text = _assemble(capped_examples)
    system_text, history = _fit_prompt(system_text, history, _assemble, ticket_id)

    # Text-only Groq call first — fast
    raw_text: str | None = await _call_groq_for_summary(system_text, history, ticket_id)

    # Multimodal fallback if the text call failed (supports images/audio)
    if raw_text is None:
        try:
            async with shared_session() as session:
                # Transcribe audio attachments via Deepgram (non-fatal)
                try:
                    transcripts = await _transcribe_audio_posts(posts, session)
                    for t in transcripts:
                        history += f"\n[Голосовое сообщение клиента: {t}]"
                except Exception as exc:
                    logger.warning("Audio transcription collection failed: %s", exc)

                # Collect images from post attachments (non-fatal)
                image_parts: list[dict] = []
                try:
                    image_parts = await _collect_image_parts(posts, session)
                    if image_parts:
                        logger.info(
                            "Including %d image(s) in vision request for ticket %s",
                            len(image_parts), ticket_id,
                        )
                except Exception as exc:
                    logger.warning("Image collection failed: %s", exc)

                raw_text = await _call_groq_vision_for_summary(
                    system_text, history, image_parts, ticket_id, session
                )
        except Exception as exc:
            logger.warning("Vision fallback session failed for ticket %s: %s", ticket_id, exc)

    if not raw_text:
        return None
    text = raw_text

    # Log raw output for future curation
    if ticket_id:
        await _log_generation(ticket_id, ticket_title, history, text)

    # Parse "Суть: ...\nКлиенту: ...\nПамятка: ..."
    import re as _re
    logger.info("AI raw response for ticket %s: %r", ticket_id, text[:400])
    text = _strip_reasoning(text)
    suit_line = ""
    client_line = ""
    memo_line = ""
    current_key: str | None = None
    for line in text.splitlines():
        # Strip markdown bold/italic (**text**, *text*) before matching
        cleaned = _re.sub(r"\*+", "", line).strip()
        lower = cleaned.lower()
        if lower.startswith("суть:"):
            suit_line = cleaned[5:].strip()
            current_key = "suit"
        elif lower.startswith("клиенту:"):
            client_line = cleaned[8:].strip()
            current_key = "client"
        elif lower.startswith("памятка:"):
            memo_line = cleaned[8:].strip()
            current_key = "memo"
        # Legacy fallback: support old "Ответ:" label
        elif lower.startswith("ответ:"):
            client_line = cleaned[6:].strip()
            current_key = "client"
        elif cleaned and current_key == "suit" and not suit_line:
            suit_line = cleaned
        elif cleaned and current_key == "client" and not client_line:
            client_line = cleaned
        elif cleaned and current_key == "memo" and not memo_line:
            memo_line = cleaned
        elif not cleaned:
            current_key = None  # blank line resets context

    if not suit_line and not client_line:
        logger.warning("Could not parse Суть/Клиенту from AI response for ticket %s", ticket_id)
        return None

    return (suit_line, client_line, memo_line, confidence_pct)


def invalidate_prompt_cache() -> None:
    """Invalidate cached active prompt (call after apply_prompt_version)."""
    global _active_prompt_loaded
    _active_prompt_loaded = False
