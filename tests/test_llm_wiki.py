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
