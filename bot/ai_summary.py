"""Generate ticket summary via Google Gemini API."""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

from .config import config

if TYPE_CHECKING:
    from .hde_api import HDEPost, HDETicketInfo

logger = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)

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


_FORMAT_INSTRUCTIONS = (
    "Ответь РОВНО двумя строками — обе обязательны:\n"
    "Строка 1: Суть: <одно предложение — в чём проблема клиента>\n"
    "Строка 2: Ответ: <конкретный краткий ответ или следующий шаг>\n\n"
    "Пример:\n"
    "Суть: Клиент не может войти в личный кабинет после смены пароля.\n"
    "Ответ: Попросите клиента очистить кэш браузера и попробовать снова.\n\n"
    "ВАЖНО: всегда пиши обе строки. Не используй markdown. Не добавляй ничего лишнего."
)


def _build_system_prompt(
    ticket_title: str,
    rag_examples: list[str] | None = None,
    wiki_context: str | None = None,
) -> str:
    base = "Ты — ассистент технической поддержки.\n"
    if ticket_title:
        base += (
            f"Тема обращения: «{ticket_title}»\n\n"
            "Используй тему и примеры, чтобы предложить конкретный ответ, "
            "подходящий именно для этого типа проблемы.\n\n"
        )
    if wiki_context:
        base += (
            "Справочная статья из базы знаний по данной теме:\n\n"
            f"{wiki_context}\n\n"
            "---\n\n"
        )
    if rag_examples:
        examples_text = "\n\n---\n\n".join(rag_examples)
        base += (
            "Вот примеры хороших ответов из вашей поддержки:\n\n"
            f"{examples_text}\n\n"
            "---\n\n"
            "Теперь обработай новый тикет:\n\n"
        )
    return base + _FORMAT_INSTRUCTIONS


def _build_history_text(posts: "list[HDEPost]", info: "HDETicketInfo") -> str:
    """Convert posts to plain text for the prompt."""
    import re
    from html import unescape

    lines: list[str] = []
    for post in posts:
        is_client = post.user_id == info.client_id
        role = "Клиент" if is_client else "Сотрудник"
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
) -> tuple[str, str] | None:
    """Return (suit_line, answer_line) tuple or None if disabled/failed."""
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

    # RAG: find similar examples from knowledge base
    rag_examples: list[str] = []
    try:
        from .knowledge.indexer import get_rag_context
        rag_examples = await get_rag_context(ticket_title, history)
    except Exception as exc:
        logger.warning("RAG context retrieval failed: %s", exc)

    # Wiki: find relevant article for this topic
    wiki_ctx: str | None = None
    try:
        from .wiki.searcher import get_wiki_context
        wiki_ctx = await get_wiki_context(ticket_title)
        if wiki_ctx:
            logger.info("Wiki context found for ticket %s (%d chars)", ticket_id, len(wiki_ctx))
    except Exception as exc:
        logger.warning("Wiki context retrieval failed: %s", exc)

    system_text = _build_system_prompt(ticket_title, rag_examples or None, wiki_ctx)

    try:
        async with aiohttp.ClientSession() as session:
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
                    "maxOutputTokens": 1000,
                },
            }

            async with session.post(
                _GEMINI_URL,
                json=payload,
                params={"key": config.gemini_api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Gemini API error %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
    except Exception as exc:
        logger.warning("Gemini request failed: %s", exc)
        return None

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Unexpected Gemini response structure: %s", exc)
        return None

    if not text:
        return None

    # Log raw output for future curation
    if ticket_id:
        _log_generation(ticket_id, ticket_title, history, text)

    # Parse "Суть: ...\nОтвет: ..."
    import re as _re
    logger.info("Gemini raw response for ticket %s: %r", ticket_id, text[:400])
    suit_line = ""
    answer_line = ""
    current_key: str | None = None
    for line in text.splitlines():
        # Strip markdown bold/italic (**text**, *text*) before matching
        cleaned = _re.sub(r"\*+", "", line).strip()
        lower = cleaned.lower()
        if lower.startswith("суть:"):
            suit_line = cleaned[5:].strip()
            current_key = "suit"
        elif lower.startswith("ответ:"):
            answer_line = cleaned[6:].strip()
            current_key = "answer"
        elif cleaned and current_key == "suit" and not suit_line:
            suit_line = cleaned  # content on next line after "Суть:"
        elif cleaned and current_key == "answer" and not answer_line:
            answer_line = cleaned  # content on next line after "Ответ:"
        elif not cleaned:
            current_key = None  # blank line resets context

    if not suit_line and not answer_line:
        logger.warning("Could not parse Суть/Ответ from Gemini response for ticket %s", ticket_id)
        return None

    return (suit_line, answer_line)
