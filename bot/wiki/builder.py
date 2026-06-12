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
from ..llm_semaphore import LLM_SEMAPHORE

logger = logging.getLogger(__name__)

_WIKI_DIR = "data/wiki"
_INDEX_PATH = f"{_WIKI_DIR}/_index.json"
_GROQ_MODEL = "llama-3.3-70b-versatile"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


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
    try:
        os.makedirs(os.path.dirname(_INDEX_PATH), exist_ok=True)
        with open(_INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.error("Wiki: could not save index: %s", exc)


def _article_path(slug: str) -> str:
    return f"{_WIKI_DIR}/{slug}.md"


async def _call_llm(
    prompt: str,
    session: aiohttp.ClientSession,
) -> str | None:
    """Send a single-turn prompt to Groq using the provided session."""
    payload = {
        "model": _GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    try:
        async with LLM_SEMAPHORE, session.post(
            _GROQ_URL,
            json=payload,
            headers={"Authorization": f"Bearer {config.groq_api_key}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("Groq wiki error %s: %s", resp.status, body[:200])
                return None
            data = await resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("Groq wiki call failed: %s", exc)
        return None


async def _extract_topic(title: str, content: str, session: aiohttp.ClientSession) -> str | None:
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        "Определи тему этого тикета — краткое название 2–5 слов на русском.\n"
        "Верни ТОЛЬКО тему, без пояснений.\n\n"
        f"Тикет: «{title}»\n\n{content[:800]}"
    )
    topic = await _call_llm(prompt, session)
    if topic:
        cleaned = re.sub(r'^[«"\']+|[»"\']+$', "", topic.strip()).strip(".")
        return cleaned if cleaned and len(cleaned) <= 80 else None
    return None


async def _create_article(topic: str, title: str, content: str, session: aiohttp.ClientSession) -> str | None:
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        f"Создай wiki-статью на тему: «{topic}»\n\n"
        "Используй тикет как основу. Формат: markdown с разделами:\n"
        "## Типичные причины\n## Решения\n## Примечания\n\n"
        "Без лишних слов, только суть.\n\n"
        f"Тикет:\nТема: {title}\n\n{content[:1500]}"
    )
    return await _call_llm(prompt, session)


async def _update_article(existing: str, title: str, content: str, session: aiohttp.ClientSession) -> str | None:
    prompt = (
        "Ты — редактор базы знаний технической поддержки.\n"
        "Обнови wiki-статью, интегрировав новые данные из тикета.\n"
        "Добавь новые паттерны или уточни существующие.\n"
        "Верни ТОЛЬКО обновлённый markdown, без пояснений.\n\n"
        f"Существующая статья:\n{existing}\n\n"
        f"---\n\nНовый тикет:\nТема: {title}\n\n{content[:1200]}"
    )
    return await _call_llm(prompt, session)


async def build_or_update_wiki_article(
    title: str,
    content: str,
    ticket_id: str = "",
) -> str | None:
    """Create or update a wiki article for the given knowledge item.

    Returns the article slug on success, None if skipped or failed.
    Errors are logged but never raised — callers should use try/except.
    """
    if not config.groq_api_key:
        logger.debug("Wiki build skipped: no Groq API key")
        return None

    os.makedirs(_WIKI_DIR, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        topic = await _extract_topic(title, content, session)
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
            article_text = await _update_article(existing, title, content, session)
            if not article_text:
                return None
            # Preserve frontmatter comments from original
            header_match = re.match(r'((?:<!--.*?-->\n)+)', existing, re.DOTALL)
            if header_match:
                header = header_match.group(1)
                body = re.sub(r'(?:<!--.*?-->\n)+', '', article_text, flags=re.DOTALL).lstrip()
                article_text = header + body
            logger.info("Wiki: updated '%s' (ticket %s)", topic, ticket_id)
        else:
            body = await _create_article(topic, title, content, session)
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
