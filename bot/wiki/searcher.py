"""LLM Wiki Searcher: find the most relevant wiki article for a ticket topic."""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_WIKI_DIR = "data/wiki"
_INDEX_PATH = f"{_WIKI_DIR}/_index.json"
_MAX_ARTICLE_CHARS = 2000


def _load_index() -> dict:
    if not os.path.exists(_INDEX_PATH):
        return {}
    try:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize(text: str) -> set[str]:
    """Extract word stems (first 6 chars) ≥4 chars for fuzzy matching."""
    words = re.findall(r'[\u0430-\u044f\u0451\u0410-\u042f\u0401a-zA-Z]+', text.lower())
    return {w[:6] for w in words if len(w) >= 4}


def _match_score(topic: str, query: str) -> int:
    """Count shared meaningful words between topic and query."""
    return len(_normalize(topic) & _normalize(query))


async def get_wiki_context(ticket_title: str) -> str | None:
    """Return the most relevant wiki article for the ticket title, or None.

    Uses word-overlap scoring — no LLM call needed.
    """
    index = _load_index()
    if not index:
        return None

    best_slug: str | None = None
    best_score = 0
    for slug, meta in index.items():
        topic = meta.get("topic", "")
        score = _match_score(topic, ticket_title)
        if score > best_score:
            best_score = score
            best_slug = slug

    if best_score == 0 or best_slug is None:
        return None

    path = f"{_WIKI_DIR}/{best_slug}.md"
    if not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        logger.warning("Wiki: could not read %s: %s", path, exc)
        return None

    # Strip HTML comments (frontmatter)
    content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL).strip()

    if len(content) > _MAX_ARTICLE_CHARS:
        content = content[:_MAX_ARTICLE_CHARS] + "\n\n[...статья сокращена]"

    return content or None
