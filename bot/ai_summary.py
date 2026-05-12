"""Generate ticket summary via Google Gemini API."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

from .config import config
from .llm_semaphore import LLM_SEMAPHORE

if TYPE_CHECKING:
    from .hde_api import HDEPost, HDETicketInfo

logger = logging.getLogger(__name__)

import json as _json
from pathlib import Path as _Path


def _load_few_shot_examples() -> list[dict]:
    path = _Path(__file__).parent / "prompts" / "few_shot_examples.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return data.get("examples", [])
    except Exception:
        return []


_FEW_SHOT_EXAMPLES: list[dict] = _load_few_shot_examples()

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "llama-3.3-70b-versatile"

_AUDIO_TYPES = {"mp3", "ogg", "wav", "m4a", "opus", "aac", "flac", "oga"}
_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

_IMAGE_TYPES = {"jpg", "jpeg", "png", "gif", "webp"}
_GEMINI_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
_MAX_IMAGES = 3          # max images per Gemini request
_MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB per image

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


async def _call_groq_for_summary(system_text: str, history: str, ticket_id: str) -> str | None:
    """Fallback: generate summary via Groq when Gemini is unavailable."""
    if not config.groq_api_key:
        logger.info("Groq fallback skipped: GROQ_API_KEY not set")
        return None
    try:
        async with LLM_SEMAPHORE, aiohttp.ClientSession() as session:
            async with session.post(
                _GROQ_URL,
                json={
                    "model": _GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": f"Переписка:\n{history}"},
                    ],
                    "max_tokens": 3000,
                    "temperature": 0.3,
                },
                headers={"Authorization": f"Bearer {config.groq_api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Groq API error %s for ticket %s: %s", resp.status, ticket_id, body[:200])
                    return None
                data = await resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        logger.info("Groq fallback succeeded for ticket %s", ticket_id)
        return text
    except Exception as exc:
        logger.warning("Groq request failed for ticket %s: %s", ticket_id, exc)
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
    "Перед ответом заполни блок рассуждения (скрыт от пользователя):\n"
    "<reasoning>\n"
    "1. Что именно сломано технически — конкретная причина, не симптом?\n"
    "2. Какая модель/ПО затронута?\n"
    "3. Какой следующий шаг оператора наиболее вероятен?\n"
    "</reasoning>\n\n"
    "Формат ответа — ровно три метки, каждая с новой строки:\n"
    "Суть: <одно предложение, 8–15 слов, что именно сломано и у какого ПО/оборудования>\n"
    "Клиенту: <одно предложение, императив, максимум 20 слов — либо один уточняющий вопрос>\n"
    "Памятка: <конкретика для специалиста — либо тире, если сказать нечего>\n\n"
    "═══════════════ «Суть» — ЖЁСТКИЕ ПРАВИЛА ═══════════════\n"
    "1. Одно предложение, 8–15 слов. Это диагноз, а не категория.\n"
    "2. Обязательно — что именно не работает + модель/ПО (Эвотор, Атол 30Ф, Posiflora, RuDesktop, Сбер-эквайринг).\n"
    "3. Без воды: не писать «клиент обратился с проблемой», «требуется диагностика», «обращение связано с».\n"
    "4. Формулируй как инженер коллеге, а не как отчёт.\n\n"
    "Хорошие примеры «Суть»:\n"
    "- «Эвотор не видит ККТ Атол 30Ф после обновления прошивки.»\n"
    "- «Posiflora не проводит оплату через терминал Сбера, ошибка связи.»\n"
    "- «X-printer 365B не печатает чеки после смены USB-порта.»\n"
    "- «Не активируется лицензия Posiflora, код активации не принимается.»\n"
    "- «Касса Атол не подключается к кассовому приложению на планшете.»\n\n"
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
    "6. Запрещено утверждать то, что ещё не произошло: «Приложение активировано», «Сейчас всё работает», «Подключаю кассу», «Пока инженер связывается». Если действие не выполнено — не пиши, что выполнено.\n"
    "7. ПРАВИЛО ВЫБОРА: если следующий шаг — собрать информацию или передать инженеру, выдай ОДИН ВОПРОС, а не инструкции. Признаки: оператор в истории просит номер телефона / ссылку / уточнение.\n"
    "8. Запретные фразы (по одной видна — переписывай): «Для дальнейшей диагностики», «необходимо выполнить следующие шаги», «позволит нашему специалисту», «Надеюсь это поможет», «Если есть вопросы», «Спасибо за обращение», «уточнить детали и согласовать».\n\n"
    "Хорошие примеры «Клиенту» (из реальных отправленных операторами):\n"
    "- «Откройте Рудесктоп, пришлите код для подключения и подготовьте компьютер для удаленного доступа.»\n"
    "- «Примите запрос на подключение в AnyDesk, подключите кассу Атол к компьютеру по USB и включите в сеть.»\n"
    "- «Проверьте IP-адрес и порт Эвотора в настройках Posiflora, затем попробуйте провести оплату.»\n"
    "- «Перезагрузите Wi-Fi роутер и кассу Эвотор кнопкой питания.»\n"
    "- «Уточните номер телефона для связи с инженером по оборудованию.»\n\n"
    "Плохие примеры (так НЕ пиши):\n"
    "- «Здравствуйте, Олеся! Специалист тех. поддержки Дина. Вижу ваш RuDesktop ID 400180391. Для дальнейшей диагностики...» → слишком длинно, представление, воды.\n"
    "- «Для подключения кассы необходимо выполнить следующие шаги:» → оборвано, штамп, нет конкретики.\n"
    "- «Нам нужно удалённо подключиться, чтобы восстановить настройки, пожалуйста, подготовьте компьютер.» → дважды вежливость, нет конкретного ПО.\n\n"
    "═══════════════ «Памятка» — ЖЁСТКИЕ ПРАВИЛА ═══════════════\n"
    "Памятка пишется ДЛЯ ОПЕРАТОРА, а не для клиента. Это шпаргалка, которая экономит ему время.\n\n"
    "Включи минимум 3 из 5 слотов — или поставь прочерк «—»:\n"
    "  (1) конкретное устройство/ПО с моделью (Атол 30Ф, Эвотор 5, X-printer 365B, RuDesktop 1.8)\n"
    "  (2) путь в меню или команда (Боковое меню → Настройки → Фискальное устройство)\n"
    "  (3) ссылка на статью БЗ из контекста (clck.ru/..., posiflora.teamly.ru/...)\n"
    "  (4) куда эскалировать (инженер по оборудованию, профильный специалист Сбер, часы 8:00–19:00)\n"
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
    "- «RuDesktop на стороне клиента • прислать ID+пароль • если зависает — переустановить с clck.ru/xxx • эскалация: инженер по оборудованию.»\n"
    "- «Эвотор 5 + Posiflora • Настройки → Банковский терминал → IP/порт • если терминал не отвечает — профильный специалист Сбер через 0321, не Сбер Бизнес.»\n"
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
        "Ты — помощник технического специалиста 2-й линии поддержки кассового оборудования.\n"
        "Специализация: АТОЛ, Эвотор, Штрих-М, Viki, фискальные регистраторы, ОФД/ФН,\n"
        "сетевые подключения, эквайринг (Сбер, ВТБ, Т-Банк).\n\n"
        "Тикет передан с 1-й линии — базовую диагностику уже провели.\n\n"
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


def _log_generation(
    ticket_id: str,
    ticket_title: str,
    history: str,
    generated: str,
) -> None:
    """Append a JSONL entry to data/ai_log.jsonl for future few-shot curation."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticket_id": ticket_id,
        "ticket_title": ticket_title,
        "history": history,
        "generated": generated,
        "operator_reply": None,
    }
    try:
        os.makedirs("data", exist_ok=True)
        with open("data/ai_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
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
    transcripts: list[str] = []
    for post in posts:
        for file_info in (post.files or []):
            data_type = (file_info.get("data_type") or "").lower().lstrip(".")
            if data_type not in _AUDIO_TYPES:
                continue
            url = file_info.get("url", "")
            if not url:
                continue
            try:
                async with session.get(
                    url, auth=auth, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Audio download failed %s: HTTP %s", url, resp.status)
                        continue
                    audio_data = await resp.read()
            except Exception as exc:
                logger.warning("Audio download error %s: %s", url, exc)
                continue
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
                        continue
                    result = await resp.json()
                    transcript = (
                        result.get("results", {})
                        .get("channels", [{}])[0]
                        .get("alternatives", [{}])[0]
                        .get("transcript", "")
                        .strip()
                    )
                    if transcript:
                        transcripts.append(transcript)
                        logger.info(
                            "Deepgram transcribed audio %s: %r...",
                            file_info.get("name"), transcript[:80],
                        )
            except Exception as exc:
                logger.warning("Deepgram transcription failed for %s: %s", url, exc)
    return transcripts


async def _collect_image_parts(
    posts: "list[HDEPost]",
    session: aiohttp.ClientSession,
) -> list[dict]:
    """Download images from post attachments, return Gemini inlineData parts."""
    parts: list[dict] = []
    auth = aiohttp.BasicAuth(config.hde_api_email, config.hde_api_key)
    for post in posts:
        for file_info in (post.files or []):
            if len(parts) >= _MAX_IMAGES:
                break
            data_type = (file_info.get("data_type") or "").lower().lstrip(".")
            if data_type not in _IMAGE_TYPES:
                continue
            url = file_info.get("url", "")
            if not url:
                continue
            try:
                async with session.get(
                    url,
                    auth=auth,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Image download failed %s: HTTP %s", url, resp.status)
                        continue
                    raw = await resp.read()
            except Exception as exc:
                logger.debug("Image download error %s: %s", url, exc)
                continue
            if len(raw) > _MAX_IMAGE_BYTES:
                logger.debug("Image too large (%d bytes), skipping %s", len(raw), url)
                continue
            mime = _GEMINI_MIME.get(data_type, "image/jpeg")
            parts.append({
                "inlineData": {
                    "mimeType": mime,
                    "data": base64.b64encode(raw).decode(),
                }
            })
            logger.debug("Added image %s (%d bytes) to Gemini request", file_info.get("name"), len(raw))
    return parts


async def generate_ticket_summary(
    posts: "list[HDEPost]",
    info: "HDETicketInfo",
    ticket_title: str = "",
    ticket_id: str = "",
    company_id: str = "",
) -> tuple[str, str, str, int] | None:
    """Return (suit_line, client_line, memo_line, confidence_pct) or None if disabled/failed."""
    if not config.gemini_api_key:
        logger.info("AI summary skipped: GEMINI_API_KEY not set")
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

    history = _build_history_text(posts, info)
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

    format_instructions = await get_active_format_instructions()
    system_text = _build_system_prompt(
        ticket_title,
        rag_examples or None,
        wiki_ctx,
        equipment=equipment,
        solution_steps=solution_steps,
        format_instructions=format_instructions,
    )

    # Groq first — much faster than Gemini (text-only)
    raw_text: str | None = await _call_groq_for_summary(system_text, history, ticket_id)

    # Gemini fallback if Groq unavailable or failed (also supports images/audio)
    if raw_text is None:
        gemini_data = None
        try:
            async with aiohttp.ClientSession() as session:
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
                            "Including %d image(s) in Gemini request for ticket %s",
                            len(image_parts), ticket_id,
                        )
                except Exception as exc:
                    logger.warning("Image collection failed: %s", exc)

                content_parts: list[dict] = [{"text": f"Переписка:\n{history}"}] + image_parts

                payload = {
                    "system_instruction": {"parts": [{"text": system_text}]},
                    "contents": [{"parts": content_parts}],
                    "generationConfig": {
                        "temperature": 0.3,
                        "maxOutputTokens": 3000,
                    },
                }

                for attempt in range(3):
                    try:
                        async with LLM_SEMAPHORE, session.post(
                            _GEMINI_URL,
                            json=payload,
                            params={"key": config.gemini_api_key},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as resp:
                            status = resp.status
                            if status in (429, 503):
                                await resp.read()  # drain connection
                            elif status != 200:
                                body = await resp.text()
                                logger.warning("Gemini API error %s for ticket %s: %s", status, ticket_id, body[:200])
                                break  # non-retryable
                            else:
                                gemini_data = await resp.json()

                        if status in (429, 503):
                            if attempt < 2:
                                wait = 65 if status == 429 else 15
                                logger.warning(
                                    "Gemini %s for ticket %s, retry in %ss (attempt %d/3)",
                                    status, ticket_id, wait, attempt + 1,
                                )
                                await asyncio.sleep(wait)
                                continue
                            logger.warning("Gemini %s after 3 attempts for ticket %s", status, ticket_id)
                            break
                        break  # success
                    except Exception as exc:
                        if attempt < 2:
                            logger.warning("Gemini request error attempt %d for ticket %s: %s", attempt + 1, ticket_id, exc)
                            await asyncio.sleep(5)
                            continue
                        logger.warning("Gemini request failed after 3 attempts for ticket %s: %s", ticket_id, exc)
                        break
        except Exception as exc:
            logger.warning("Gemini session failed for ticket %s: %s", ticket_id, exc)

        if gemini_data is not None:
            try:
                raw_text = gemini_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError, TypeError) as exc:
                logger.warning("Unexpected Gemini response structure for ticket %s: %s", ticket_id, exc)

    if not raw_text:
        return None
    text = raw_text

    # Log raw output for future curation
    if ticket_id:
        _log_generation(ticket_id, ticket_title, history, text)

    # Parse "Суть: ...\nКлиенту: ...\nПамятка: ..."
    import re as _re
    logger.info("AI raw response for ticket %s: %r", ticket_id, text[:400])
    text = _re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=_re.DOTALL).strip()
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
