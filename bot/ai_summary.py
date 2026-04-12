"""Generate ticket summary via Google Gemini API."""
from __future__ import annotations

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

_FORMAT_INSTRUCTIONS = (
    "Ответь СТРОГО в формате:\n"
    "Суть: <одно предложение, что хочет клиент>\n"
    "Ответ: <краткий предложенный ответ клиенту>\n\n"
    "Не добавляй ничего лишнего. Не используй markdown."
)


def _build_system_prompt(ticket_title: str, rag_examples: list[str] | None = None) -> str:
    base = "Ты — ассистент технической поддержки.\n"
    if ticket_title:
        base += (
            f"Тема обращения: «{ticket_title}»\n\n"
            "Используй тему и примеры, чтобы предложить конкретный ответ, "
            "подходящий именно для этого типа проблемы.\n\n"
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


async def generate_ticket_summary(
    posts: "list[HDEPost]",
    info: "HDETicketInfo",
    ticket_title: str = "",
    ticket_id: str = "",
) -> str | None:
    """Return formatted summary string or None if disabled/failed."""
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

    system_text = _build_system_prompt(ticket_title, rag_examples or None)

    payload = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": [{"parts": [{"text": f"Переписка:\n{history}"}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 800,
        },
    }

    try:
        async with aiohttp.ClientSession() as session:
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
    suit_line = ""
    answer_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("суть:"):
            suit_line = stripped[5:].strip()
        elif stripped.lower().startswith("ответ:"):
            answer_line = stripped[6:].strip()

    if not suit_line and not answer_line:
        # Fallback: show raw text
        return f"🧠 <b>AI Саммари</b>\n\n{text}"

    from html import escape
    parts = ["🧠 <b>AI Саммари</b>"]
    if suit_line:
        parts.append(f"\n<b>Суть:</b> {escape(suit_line)}")
    if answer_line:
        parts.append(f"\n💡 <b>Предложенный ответ:</b>\n<i>«{escape(answer_line)}»</i>")

    return "\n".join(parts)
