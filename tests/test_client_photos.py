"""Фото клиента → описание Vision в photo_descriptions до генерации черновика."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.hde_api import HDEAttachment
from bot.config import config

_INFO = SimpleNamespace(client_id=7)


def _png(file_hash: str, name: str = "image.png") -> dict:
    return {
        "name": name,
        "url": f"https://hde.example.com/ru/file/download/{file_hash}",
        "data_type": "png",
    }


def _fakes(topic_hashes: str = "", *, describe=lambda content: "Ошибка 3807", big: set = frozenset()):
    calls = {"downloaded": [], "described": [], "stored": []}

    async def topic_fn(ticket_id):
        return SimpleNamespace(photo_hashes=topic_hashes)

    async def download_fn(ticket_id, file_hash):
        calls["downloaded"].append(file_hash)
        size = 5 * 1024 * 1024 if file_hash in big else 10
        return HDEAttachment(filename=file_hash, content=b"x" * size, content_type="image/webp")

    async def describe_fn(content, filename="", mime=""):
        calls["described"].append(mime)
        return describe(content)

    async def store_fn(ticket_id, descriptions, hashes=()):
        calls["stored"].append((list(descriptions), list(hashes)))

    return calls, dict(_topic_fn=topic_fn, _download_fn=download_fn,
                       _describe_fn=describe_fn, _store_fn=store_fn)


@pytest.mark.asyncio
async def test_only_new_client_photos_are_described_and_marked():
    from bot.agent.context import describe_client_photos

    posts = [
        SimpleNamespace(user_id=7, is_comment=False, files=[_png("h-old"), _png("h-new")]),
        SimpleNamespace(user_id=99, is_comment=False, files=[_png("h-staff")]),
        SimpleNamespace(user_id=7, is_comment=True, files=[_png("h-comment")]),
        SimpleNamespace(user_id=7, is_comment=False,
                        files=[{"name": "a.pdf", "url": "https://x/ru/file/download/h-pdf", "data_type": "pdf"}]),
    ]
    calls, fakes = _fakes("h-old")

    await describe_client_photos("T1", posts, _INFO, **fakes)

    assert calls["downloaded"] == ["h-new"]
    assert calls["described"] == ["image/webp"]   # тип из ответа HDE, не из имени .png
    assert calls["stored"] == [(["Ошибка 3807"], ["h-new"])]


@pytest.mark.asyncio
async def test_failed_vision_is_retried_next_time_but_oversized_is_not():
    from bot.agent.context import describe_client_photos

    posts = [SimpleNamespace(user_id=7, is_comment=False, files=[_png("h-fail"), _png("h-big")])]
    calls, fakes = _fakes(describe=lambda content: None, big={"h-big"})

    await describe_client_photos("T1", posts, _INFO, **fakes)

    assert calls["described"] == ["image/webp"]          # огромное в Groq не ушло
    assert calls["stored"] == [([], ["h-big"])]          # h-fail не помечен — повторим


@pytest.mark.asyncio
async def test_no_client_photos_touches_nothing():
    from bot.agent.context import describe_client_photos

    posts = [SimpleNamespace(user_id=7, text="касса не печатает")]   # без files вовсе
    calls, fakes = _fakes()

    await describe_client_photos("T1", posts, _INFO, **fakes)

    assert calls == {"downloaded": [], "described": [], "stored": []}


@pytest.mark.asyncio
async def test_append_photo_descriptions_records_hashes(initialized_db):
    import bot.db as db_module

    await db_module.upsert_topic("T-PH", 1, chat_id=config.group_chat_id)
    await db_module.append_photo_descriptions("T-PH", ["Ошибка 3807"], hashes=["h1"])
    await db_module.append_photo_descriptions("T-PH", [], hashes=["h2"])

    rec = await db_module.get_topic("T-PH")
    assert rec.photo_descriptions == "Ошибка 3807"
    assert rec.photo_hashes.split() == ["h1", "h2"]
