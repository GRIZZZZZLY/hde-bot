"""LLM Wiki Builder: synthesize knowledge items into structured markdown articles."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

import aiohttp

from ..config import config

logger = logging.getLogger(__name__)

_WIKI_DIR = "data/wiki"
_INDEX_PATH = f"{_WIKI_DIR}/_index.json"
_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)


def _topic_slug(topic: str) -> str:
    """Convert topic name to a stable 12-char hex slug (MD5-based)."""
    return hashlib.md5(topic.strip().lower().encode()).hexdigest()[:12]


def _load_index() -> dict:
    """Load wiki index (slug -> {topic, updated})."""
    if not os.path.exists(_INDEX_PATH):
        return {}
    try:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(index: dict) -> None:
    os.makedirs(os.path.dirname(_INDEX_PATH), exist_ok=True)
    with open(_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def _article_path(slug: str) -> str:
    return f"{_WIKI_DIR}/{slug}.md"


async def _call_gemini(prompt: str) -> str | None:
    """Send a single-turn prompt to Gemini and return text or None."""
    if not config.gemini_api_key:
        return None
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000},
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
                    logger.warning("Gemini wiki error %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        logger.warning("Gemini wiki call failed: %s", exc)
        return None


async def _extract_topic(title: str, content: str) -> str | None:
    """Ask Gemini for a canonical topic name (2-5 words in Russian)."""
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        "Определи тему этого тикета — краткое название 2–5 слов на русском.\n"
        "Верни ТОЛЬКО тему, без пояснений.\n\n"
        f"Тикет: «{title}»\n\n{content[:800]}"
    )
    topic = await _call_gemini(prompt)
    if topic:
        return re.sub(r'^[«"\']+|[»"\']+$', "", topic.strip())
    return None


async def _create_article(topic: str, title: str, content: str) -> str | None:
    """Ask Gemini to write a new wiki article from a ticket."""
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        f"Создай wiki-статью на тему: «{topic}»\n\n"
        "Используй тикет как основу. Формат: markdown с разделами:\n"
        "## Типичные причины\n## Решения\n## Примечания\n\n"
        "Без лишних слов, только суть.\n\n"
        f"Тикет:\nТема: {title}\n\n{content[:1500]}"
    )
    return await _call_gemini(prompt)


async def _update_article(existing: str, title: str, content: str) -> str | None:
    """Ask Gemini to update an existing wiki article with new information."""
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        "Обнови wiki-статью, интегрировав новые данные из тикета.\n"
        "Добавь новые паттерны или уточни существующие.\n"
        "Верни ТОЛЬКО обновлённый markdown, без пояснений.\n\n"
        f"Существующая статья:\n{existing}\n\n"
        f"---\n\nНовый тикет:\nТема: {title}\n\n{content[:1200]}"
    )
    return await _call_gemini(prompt)


async def build_or_update_wiki_article(
    title: str,
    content: str,
    ticket_id: str = "",
) -> str | None:
    """Create or update a wiki article for the given knowledge item.

    Returns the article slug on success, None if skipped or failed.
    Errors are logged but never raised — callers should use try/except.
    """
    if not config.gemini_api_key:
        logger.debug("Wiki build skipped: no Gemini API key")
        return None

    os.makedirs(_WIKI_DIR, exist_ok=True)

    topic = await _extract_topic(title, content)
    if not topic:
        logger.warning("Wiki: could not extract topic for ticket %s", ticket_id)
        return None

    slug = _topic_slug(topic)
    path = _article_path(slug)

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            existing = ""
        article_text = await _update_article(existing, title, content)
        if not article_text:
            return None
        logger.info("Wiki: updated '%s' (ticket %s)", topic, ticket_id)
    else:
        body = await _create_article(topic, title, content)
        if not body:
            return None
        now = datetime.now(timezone.utc).isoformat()
        article_text = (
            f"<!-- topic: {topic} -->\n"
            f"<!-- created: {now} -->\n\n"
            f"# {topic}\n\n{body}"
        )
        logger.info("Wiki: created '%s' (ticket %s)", topic, ticket_id)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(article_text)
    except OSError as exc:
        logger.error("Wiki: could not write %s: %s", path, exc)
        return None

    index = _load_index()
    index[slug] = {"topic": topic, "updated": datetime.now(timezone.utc).isoformat()}
    _save_index(index)

    return slug
