"""Tests for LLM Wiki builder and searcher."""
from __future__ import annotations
import json
import os
import pytest


def test_topic_slug_stable():
    from bot.wiki.builder import _topic_slug
    assert _topic_slug("Авторизация") == _topic_slug("Авторизация")
    assert _topic_slug("авторизация") == _topic_slug("АВТОРИЗАЦИЯ")  # case-insensitive
    assert len(_topic_slug("any topic")) == 12


def test_topic_slug_different_topics():
    from bot.wiki.builder import _topic_slug
    assert _topic_slug("Авторизация") != _topic_slug("Принтер")


def test_load_index_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.wiki.builder._INDEX_PATH", str(tmp_path / "missing.json"))
    from bot.wiki.builder import _load_index
    assert _load_index() == {}


def test_save_and_load_index(tmp_path, monkeypatch):
    index_path = str(tmp_path / "index.json")
    wiki_dir = str(tmp_path)
    monkeypatch.setattr("bot.wiki.builder._INDEX_PATH", index_path)
    monkeypatch.setattr("bot.wiki.builder._WIKI_DIR", wiki_dir)
    from bot.wiki.builder import _save_index, _load_index
    data = {"abc123": {"topic": "Авторизация", "updated": "2026-04-12T00:00:00"}}
    _save_index(data)
    loaded = _load_index()
    assert loaded["abc123"]["topic"] == "Авторизация"


def test_match_score_no_overlap():
    from bot.wiki.searcher import _match_score
    assert _match_score("принтер АТОЛ", "кассовый аппарат") == 0


def test_match_score_overlap():
    from bot.wiki.searcher import _match_score
    assert _match_score("ошибка авторизации пароль", "авторизация пользователя") > 0


@pytest.mark.asyncio
async def test_get_wiki_context_no_index(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.wiki.searcher._INDEX_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr("bot.wiki.searcher._WIKI_DIR", str(tmp_path))
    from bot.wiki.searcher import get_wiki_context
    result = await get_wiki_context("авторизация")
    assert result is None


@pytest.mark.asyncio
async def test_get_wiki_context_finds_article(tmp_path, monkeypatch):
    import json
    index_path = str(tmp_path / "_index.json")
    wiki_dir = str(tmp_path)
    monkeypatch.setattr("bot.wiki.searcher._INDEX_PATH", index_path)
    monkeypatch.setattr("bot.wiki.searcher._WIKI_DIR", wiki_dir)

    slug = "abc123def456"
    index = {slug: {"topic": "Авторизация пароль", "updated": "2026-04-12T00:00:00"}}
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    article_path = tmp_path / f"{slug}.md"
    article_path.write_text("## Решения\nОчистить кэш браузера.", encoding="utf-8")

    from bot.wiki.searcher import get_wiki_context
    result = await get_wiki_context("проблема с авторизацией паролем")
    assert result is not None
    assert "Очистить кэш" in result


@pytest.mark.asyncio
async def test_get_wiki_context_no_match(tmp_path, monkeypatch):
    import json
    index_path = str(tmp_path / "_index.json")
    wiki_dir = str(tmp_path)
    monkeypatch.setattr("bot.wiki.searcher._INDEX_PATH", index_path)
    monkeypatch.setattr("bot.wiki.searcher._WIKI_DIR", wiki_dir)

    slug = "abc123def456"
    index = {slug: {"topic": "Принтер АТОЛ", "updated": "2026-04-12T00:00:00"}}
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    article_path = tmp_path / f"{slug}.md"
    article_path.write_text("## Решения\nПереустановить драйвер.", encoding="utf-8")

    from bot.wiki.searcher import get_wiki_context
    result = await get_wiki_context("проблема с кассой")
    assert result is None


@pytest.mark.asyncio
async def test_get_wiki_context_strips_frontmatter(tmp_path, monkeypatch):
    import json
    index_path = str(tmp_path / "_index.json")
    wiki_dir = str(tmp_path)
    monkeypatch.setattr("bot.wiki.searcher._INDEX_PATH", index_path)
    monkeypatch.setattr("bot.wiki.searcher._WIKI_DIR", wiki_dir)

    slug = "abc123def456"
    index = {slug: {"topic": "Авторизация пароль", "updated": "2026-04-12T00:00:00"}}
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    article_path = tmp_path / f"{slug}.md"
    article_path.write_text(
        "<!-- topic: Авторизация пароль -->\n<!-- created: 2026-04-12 -->\n\n# Авторизация\n\nТекст.",
        encoding="utf-8"
    )

    from bot.wiki.searcher import get_wiki_context
    result = await get_wiki_context("авторизация пароль")
    assert result is not None
    assert "<!-- topic" not in result
    assert "Текст." in result
