"""URL-майнинг: ссылки из ответов операторов (dialogue_pairs) → knowledge_items.

Операторы часто решают тикет ссылкой на статью (support.evotor.ru, teamly и
т.п.) — бот этих статей не знает и предлагает своё. Достаём URL прямо из уже
намайненных пар (ноль запросов к HDE API), качаем топ по частоте, индексируем
в RAG. Дедуп — по хэшу URL, повторный прогон не плодит дубли."""
from __future__ import annotations

import hashlib
import html as _html
import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")
_TRAILING_PUNCT = ".,;:!?»«\"'"

# Мессенджер-ссылки — контакты, не статьи; шортенеры на них режутся по
# финальному хосту после редиректов в fetch_article.
_SKIP_DOMAINS = {
    "wa.me", "t.me", "chat.whatsapp.com", "api.whatsapp.com",
    "web.whatsapp.com",
}


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1).lower() if m else ""

# Страница короче — скорее всего заглушка/редирект/ошибка, в базу не годится.
_MIN_TEXT_CHARS = 200

_FETCH_TIMEOUT_S = 20


def extract_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(_TRAILING_PUNCT) for m in _URL_RE.finditer(text or "")]


def rank_urls(answers: list[str], top_n: int = 30) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for answer in answers:
        counter.update(
            u for u in extract_urls(answer) if _domain(u) not in _SKIP_DOMAINS
        )
    return counter.most_common(top_n)


def url_hash(url: str) -> str:
    return hashlib.sha256(f"operator_url:{url}".encode()).hexdigest()


def html_to_text(raw: str) -> tuple[str, str]:
    """(title, text) из HTML: без script/style/тегов, entity разэкранированы."""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    title = _html.unescape(m.group(1)).strip() if m else ""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = _html.unescape(body)
    text = re.sub(r"\s+", " ", body).strip()
    return title, text


async def fetch_article(url: str) -> tuple[str, str] | None:
    """(title, text) страницы или None (не HTML / ошибка / слишком коротко)."""
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                if (resp.url.host or "").lower() in _SKIP_DOMAINS:
                    return None  # шортенер привёл в мессенджер
                ctype = resp.headers.get("Content-Type", "")
                if "html" not in ctype and "text" not in ctype:
                    return None
                raw = await resp.text(errors="ignore")
    except Exception as exc:
        logger.warning("url_mining: fetch %s failed: %s", url, exc)
        return None
    title, text = html_to_text(raw)
    if len(text) < _MIN_TEXT_CHARS:
        return None
    return title, text[:20000]


async def mine_operator_urls(
    top_n: int = 30, *, _answers_fn=None, _fetch_fn=None, _index_fn=None,
    _hashes_fn=None,
) -> dict:
    """Топ-N ссылок из ответов операторов → скачать → в knowledge_items."""
    if _answers_fn is None:
        from ..db import list_operator_answers_with_urls as _answers_fn
    if _fetch_fn is None:
        _fetch_fn = fetch_article
    if _index_fn is None:
        from .indexer import index_knowledge_item as _index_fn
    if _hashes_fn is None:
        from ..db import list_knowledge_content_hashes as _hashes_fn

    known = await _hashes_fn()
    stats = {"candidates": 0, "indexed": 0, "known": 0, "failed": 0}
    for url, count in rank_urls(await _answers_fn(), top_n):
        stats["candidates"] += 1
        h = url_hash(url)
        if h in known:
            stats["known"] += 1
            continue
        article = await _fetch_fn(url)
        if article is None:
            stats["failed"] += 1
            continue
        title, text = article
        item_id = await _index_fn(
            "operator_url",
            f"{title}\n\n{text}" if title else text,
            title=title or url,
            url=url,
            content_hash=h,
        )
        if item_id is None:
            stats["failed"] += 1
        else:
            stats["indexed"] += 1
            logger.info("url_mining: indexed %s (%d refs)", url, count)
    return stats
