"""Tests for bot/vision.py (Groq Llama 4 Scout image description)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_session(resp_body: dict) -> MagicMock:
    mock_resp = AsyncMock()
    mock_resp.json = AsyncMock(return_value=resp_body)
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return mock_session


@pytest.mark.asyncio
async def test_describe_image_returns_text():
    """Groq returns choices → describe_image returns trimmed text."""
    from bot import vision

    resp_body = {
        "choices": [{"message": {"content": "  Ошибка ФН 234  "}}]
    }
    mock_session = _mock_session(resp_body)

    with patch("bot.vision.aiohttp.ClientSession", return_value=mock_session), \
         patch("bot.vision.config") as mock_config:
        mock_config.groq_api_key = "fake-key"
        mock_config.groq_vision_model = "vision/model-from-env"
        result = await vision.describe_image(b"\x89PNG\r\n\x1a\n fake", "photo.png")

    assert result == "Ошибка ФН 234"
    # Image must go as OpenAI-style data URI to the vision model from env
    payload = mock_session.post.call_args.kwargs["json"]
    assert payload["model"] == "vision/model-from-env"
    content = payload["messages"][0]["content"]
    image_parts = [p for p in content if p.get("type") == "image_url"]
    assert image_parts
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_describe_image_no_api_key_returns_none():
    from bot import vision

    with patch("bot.vision.config") as mock_config:
        mock_config.groq_api_key = ""
        result = await vision.describe_image(b"bytes", "a.jpg")

    assert result is None


@pytest.mark.asyncio
async def test_describe_image_empty_bytes_returns_none():
    from bot import vision

    with patch("bot.vision.config") as mock_config:
        mock_config.groq_api_key = "fake-key"
        result = await vision.describe_image(b"", "a.jpg")

    assert result is None


@pytest.mark.asyncio
async def test_describe_image_oversized_returns_none():
    from bot import vision

    big = b"x" * (vision._MAX_BYTES + 1)
    with patch("bot.vision.config") as mock_config:
        mock_config.groq_api_key = "fake-key"
        result = await vision.describe_image(big, "big.jpg")

    assert result is None


@pytest.mark.asyncio
async def test_describe_image_error_response_returns_none():
    """Groq error payload → describe_image returns None (non-fatal)."""
    from bot import vision

    resp_body = {"error": {"message": "quota exceeded"}}
    mock_session = _mock_session(resp_body)

    with patch("bot.vision.aiohttp.ClientSession", return_value=mock_session), \
         patch("bot.vision.config") as mock_config:
        mock_config.groq_api_key = "fake-key"
        result = await vision.describe_image(b"bytes", "a.jpg")

    assert result is None


@pytest.mark.asyncio
async def test_llm_semaphore_limits_concurrency():
    """LLM_SEMAPHORE blocks more than LLM_CONCURRENCY concurrent acquisitions."""
    import asyncio
    from bot.llm_semaphore import LLM_SEMAPHORE, LLM_CONCURRENCY

    assert LLM_CONCURRENCY == 3
    counter = {"active": 0, "max": 0}
    release = asyncio.Event()

    async def worker():
        async with LLM_SEMAPHORE:
            counter["active"] += 1
            counter["max"] = max(counter["max"], counter["active"])
            await release.wait()
            counter["active"] -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(6)]
    await asyncio.sleep(0.05)  # let them race
    assert counter["max"] <= LLM_CONCURRENCY
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_describe_image_mime_from_response_wins_over_name():
    """HDE отдаёт webp под именем image.png — тип берётся из ответа."""
    from bot import vision

    mock_session = _mock_session({"choices": [{"message": {"content": "ok"}}]})
    with patch("bot.vision.aiohttp.ClientSession", return_value=mock_session), \
         patch("bot.vision.config") as mock_config:
        mock_config.groq_api_key = "fake-key"
        await vision.describe_image(b"RIFF fake", "image.png", mime="image/webp")
        await vision.describe_image(b"RIFF fake", "image.png", mime="application/octet-stream")

    urls = [
        call.kwargs["json"]["messages"][0]["content"][1]["image_url"]["url"]
        for call in mock_session.post.call_args_list
    ]
    assert urls[0].startswith("data:image/webp;base64,")
    assert urls[1].startswith("data:image/png;base64,")   # не картинка → по имени


def test_guess_mime_variants():
    from bot.vision import _guess_mime

    assert _guess_mime("x.png") == "image/png"
    assert _guess_mime("X.PNG") == "image/png"
    assert _guess_mime("x.webp") == "image/webp"
    assert _guess_mime("x.gif") == "image/gif"
    assert _guess_mime("x.jpg") == "image/jpeg"
    assert _guess_mime("unknown") == "image/jpeg"


# --- Priority 2 Variant Б: persistence + RAG augmentation ---


@pytest.mark.asyncio
async def test_append_photo_descriptions_accumulates(initialized_db):
    """append_photo_descriptions appends to existing value, newline-joined."""
    import bot.db as db_module
    from unittest.mock import AsyncMock as _AM
    from aiogram.types import BufferedInputFile  # noqa: F401  (ensure import ok)
    from bot.topic_manager import handle_assigned_on_create

    # create topic via normal flow
    bot = _AM()
    forum_topic = type("F", (), {"message_thread_id": 42})()
    bot.create_forum_topic = _AM(return_value=forum_topic)
    bot.send_message = _AM()
    payload = {
        "ticket_id": "TKT-VISION-1", "unique_id": "U-VIS-1", "ticket_name": "Фото",
        "company_name": "ACME", "priority": "medium", "status": "open",
        "owner_id": "me", "owner_name": "Me", "user_name": "Alice",
        "message": "msg", "last_post_date": "2026-04-16 12:00:00",
        "sla_remaining_minutes": "30", "link": "https://hde.example.com/t/1",
    }
    import bot.topic_manager as tm
    monkey_was = tm._is_work_time
    tm._is_work_time = lambda *a, **k: True
    try:
        await handle_assigned_on_create(bot, payload)
    finally:
        tm._is_work_time = monkey_was

    await db_module.append_photo_descriptions("TKT-VISION-1", ["Ошибка ФН 234"])
    rec = await db_module.get_topic("TKT-VISION-1")
    assert "Ошибка ФН 234" in (rec.photo_descriptions or "")

    await db_module.append_photo_descriptions(
        "TKT-VISION-1", ["Чек с датой 01.04", "Экран кассы"]
    )
    rec = await db_module.get_topic("TKT-VISION-1")
    desc = rec.photo_descriptions or ""
    assert "Ошибка ФН 234" in desc
    assert "Чек с датой 01.04" in desc
    assert "Экран кассы" in desc


@pytest.mark.asyncio
async def test_append_photo_descriptions_empty_is_noop(initialized_db):
    """Empty list / whitespace-only descriptions → no write, no crash."""
    import bot.db as db_module

    # No topic exists → no-op, no error
    await db_module.append_photo_descriptions("DOES-NOT-EXIST", [])
    await db_module.append_photo_descriptions("DOES-NOT-EXIST", ["", "   "])


@pytest.mark.asyncio
async def test_index_knowledge_item_appends_photo_descriptions(initialized_db, monkeypatch):
    """index_knowledge_item augments content with stored photo descriptions."""
    import bot.db as db_module
    from bot.knowledge import indexer as indexer_mod
    from bot.topic_manager import handle_assigned_on_create
    import bot.topic_manager as tm
    from unittest.mock import AsyncMock as _AM

    bot = _AM()
    forum_topic = type("F", (), {"message_thread_id": 77})()
    bot.create_forum_topic = _AM(return_value=forum_topic)
    bot.send_message = _AM()
    payload = {
        "ticket_id": "TKT-VIS-2", "unique_id": "U-VIS-2", "ticket_name": "T",
        "company_name": "ACME", "priority": "medium", "status": "open",
        "owner_id": "me", "owner_name": "Me", "user_name": "Alice",
        "message": "msg", "last_post_date": "2026-04-16 12:00:00",
        "sla_remaining_minutes": "30", "link": "https://hde.example.com/t/2",
    }
    orig = tm._is_work_time
    tm._is_work_time = lambda *a, **k: True
    try:
        await handle_assigned_on_create(bot, payload)
    finally:
        tm._is_work_time = orig

    await db_module.append_photo_descriptions("TKT-VIS-2", ["Экран с ошибкой ФН 234"])

    captured: dict = {}

    async def fake_upsert(**kw):
        captured.update(kw)
        return 1, True

    async def fake_embed(text, task_type="passage"):
        captured["embed_text"] = text
        return None  # skip embedding step

    monkeypatch.setattr(indexer_mod.db, "upsert_knowledge_item", fake_upsert)
    monkeypatch.setattr(indexer_mod, "embed_text", fake_embed)

    await indexer_mod.index_knowledge_item(
        source="hde_closed",
        content="История: замена ФН",
        ticket_id="TKT-VIS-2",
        title="Замена ФН",
    )

    assert "История: замена ФН" in captured["content"]
    assert "Описания прикреплённых фото:" in captured["content"]
    assert "Экран с ошибкой ФН 234" in captured["content"]
    assert captured["embed_text"].endswith("Экран с ошибкой ФН 234")


@pytest.mark.asyncio
async def test_index_knowledge_item_without_photos_unchanged(initialized_db, monkeypatch):
    """Ticket with no photo descriptions → content passes through unchanged."""
    import bot.db as db_module
    from bot.knowledge import indexer as indexer_mod
    from bot.topic_manager import handle_assigned_on_create
    import bot.topic_manager as tm
    from unittest.mock import AsyncMock as _AM

    bot = _AM()
    forum_topic = type("F", (), {"message_thread_id": 78})()
    bot.create_forum_topic = _AM(return_value=forum_topic)
    bot.send_message = _AM()
    payload = {
        "ticket_id": "TKT-VIS-3", "unique_id": "U-VIS-3", "ticket_name": "T3",
        "company_name": "ACME", "priority": "medium", "status": "open",
        "owner_id": "me", "owner_name": "Me", "user_name": "Alice",
        "message": "msg", "last_post_date": "2026-04-16 12:00:00",
        "sla_remaining_minutes": "30", "link": "https://hde.example.com/t/3",
    }
    orig = tm._is_work_time
    tm._is_work_time = lambda *a, **k: True
    try:
        await handle_assigned_on_create(bot, payload)
    finally:
        tm._is_work_time = orig

    captured: dict = {}

    async def fake_upsert(**kw):
        captured.update(kw)
        return 2, True

    async def fake_embed(text, task_type="passage"):
        return None

    monkeypatch.setattr(indexer_mod.db, "upsert_knowledge_item", fake_upsert)
    monkeypatch.setattr(indexer_mod, "embed_text", fake_embed)

    await indexer_mod.index_knowledge_item(
        source="hde_closed",
        content="Просто история",
        ticket_id="TKT-VIS-3",
    )

    assert captured["content"] == "Просто история"
    assert "Описания прикреплённых фото" not in captured["content"]
