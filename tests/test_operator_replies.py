from types import SimpleNamespace

import pytest

import bot.db as db_module
from bot.config import config
from bot.hde_api import HDEApiClient
from bot.operator_replies import (
    OperatorReplyError,
    add_internal_note,
    cache_incoming_topic_media,
    get_operator_topic_context,
    send_public_reply,
    edit_operator_message,
    delete_operator_message,
)


class DummyBot:
    def __init__(self):
        self.payloads: dict[str, bytes] = {}
        self.download_calls: list[str] = []
        self.sent_messages: list[tuple] = []
        self.closed_topics: list[int] = []

    async def download(self, file, destination, **kwargs):
        self.download_calls.append(file)
        destination.write(self.payloads.get(file, b"file-bytes"))
        destination.seek(0)
        return destination

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))

    async def delete_forum_topic(self, chat_id, message_thread_id, **kwargs):
        self.closed_topics.append(message_thread_id)


def make_message(
    *,
    message_id: int = 1,
    message_thread_id: int | None = 9001,
    media_group_id: str | None = None,
    text: str | None = None,
    caption: str | None = None,
    reply_to_message=None,
    photo=None,
    document=None,
    video=None,
    voice=None,
    audio=None,
    animation=None,
    video_note=None,
    from_user=None,
):
    return SimpleNamespace(
        message_id=message_id,
        message_thread_id=message_thread_id,
        media_group_id=media_group_id,
        text=text,
        caption=caption,
        reply_to_message=reply_to_message,
        photo=photo,
        document=document,
        video=video,
        voice=voice,
        audio=audio,
        animation=animation,
        video_note=video_note,
        from_user=from_user or SimpleNamespace(is_bot=False),
    )


@pytest.fixture
async def active_topic(initialized_db):
    await db_module.upsert_topic(
        "TKT-100",
        9001,
        unique_id="ABC-100",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="me",
        owner_name="Me",
        hde_link="https://hde.example.com/tickets/100",
    )
    return await get_operator_topic_context(
        telegram_user_id=config.personal_chat_id,
        topic_id=9001,
    )


@pytest.mark.asyncio
async def test_cache_incoming_topic_media_stores_album_entry(initialized_db):
    message = make_message(
        message_id=101,
        media_group_id="group-1",
        caption="Album caption",
        photo=[SimpleNamespace(file_id="photo-small"), SimpleNamespace(file_id="photo-big")],
    )

    await cache_incoming_topic_media(message)

    items = await db_module.list_cached_topic_media_group(9001, "group-1")
    assert len(items) == 1
    assert items[0].file_id == "photo-big"
    assert items[0].text == "Album caption"


@pytest.mark.asyncio
async def test_add_internal_note_calls_hde_api(active_topic, monkeypatch):
    calls = []

    async def fake_add_comment(self, ticket_id: str, text: str = "", attachments=()):
        calls.append((ticket_id, text, attachments))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient.add_comment", fake_add_comment)

    result = await add_internal_note(
        bot=DummyBot(),
        context=active_topic,
        message=make_message(text="/note Internal note"),
        command_args="Internal note",
    )

    assert "ABC-100" in result
    assert calls == [("TKT-100", "Internal note", ())]


@pytest.mark.asyncio
async def test_add_internal_note_sends_replied_photo(active_topic, monkeypatch):
    calls = []

    async def fake_add_comment(self, ticket_id: str, text: str = "", attachments=()):
        calls.append((ticket_id, text, attachments))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient.add_comment", fake_add_comment)
    bot = DummyBot()
    bot.payloads["photo-big"] = b"image-bytes"
    photo_message = make_message(
        caption="Caption from photo",
        photo=[SimpleNamespace(file_id="photo-small"), SimpleNamespace(file_id="photo-big")],
    )

    await add_internal_note(
        bot=bot,
        context=active_topic,
        message=make_message(text="/note", reply_to_message=photo_message),
        command_args=None,
    )

    assert bot.download_calls == ["photo-big"]
    assert calls[0][0] == "TKT-100"
    assert calls[0][1] == "Caption from photo"
    assert len(calls[0][2]) == 1
    assert calls[0][2][0].filename == "photo.jpg"
    assert calls[0][2][0].content == b"image-bytes"


@pytest.mark.asyncio
async def test_send_public_reply_requires_text_or_media(active_topic):
    with pytest.raises(OperatorReplyError):
        await send_public_reply(
            bot=DummyBot(),
            context=active_topic,
            message=make_message(text="/send"),
            command_args=None,
        )


@pytest.mark.asyncio
async def test_send_public_reply_calls_hde_api(active_topic, monkeypatch):
    calls = []

    async def fake_add_post(self, ticket_id: str, text: str = "", attachments=()):
        calls.append((ticket_id, text, attachments))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient.add_post", fake_add_post)

    result = await send_public_reply(
        bot=DummyBot(),
        context=active_topic,
        message=make_message(text="/send Public reply"),
        command_args="Public reply",
    )

    assert "ABC-100" in result
    assert "HDE" in result
    assert calls == [("TKT-100", "Public reply", ())]


@pytest.mark.asyncio
async def test_send_public_reply_sends_voice_attachment(active_topic, monkeypatch):
    calls = []

    async def fake_add_post(self, ticket_id: str, text: str = "", attachments=()):
        calls.append((ticket_id, text, attachments))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient.add_post", fake_add_post)
    bot = DummyBot()
    bot.payloads["voice-id"] = b"voice-bytes"
    voice_message = make_message(
        caption="Voice caption",
        voice=SimpleNamespace(file_id="voice-id", mime_type="audio/ogg"),
    )

    await send_public_reply(
        bot=bot,
        context=active_topic,
        message=make_message(text="/send", reply_to_message=voice_message),
        command_args=None,
    )

    assert bot.download_calls == ["voice-id"]
    assert calls[0][0] == "TKT-100"
    assert calls[0][1] == "Voice caption"
    assert len(calls[0][2]) == 1
    assert calls[0][2][0].filename == "voice.ogg"
    assert calls[0][2][0].content_type == "audio/ogg"
    assert calls[0][2][0].content == b"voice-bytes"


@pytest.mark.asyncio
async def test_send_public_reply_collects_cached_album(active_topic, monkeypatch):
    calls = []

    async def fake_add_post(self, ticket_id: str, text: str = "", attachments=()):
        calls.append((ticket_id, text, attachments))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient.add_post", fake_add_post)
    bot = DummyBot()
    bot.payloads["album-photo-1"] = b"img-1"
    bot.payloads["album-photo-2"] = b"img-2"

    album_first = make_message(
        message_id=201,
        media_group_id="album-42",
        caption="Album caption",
        photo=[SimpleNamespace(file_id="album-photo-1-small"), SimpleNamespace(file_id="album-photo-1")],
    )
    album_second = make_message(
        message_id=202,
        media_group_id="album-42",
        photo=[SimpleNamespace(file_id="album-photo-2-small"), SimpleNamespace(file_id="album-photo-2")],
    )
    await cache_incoming_topic_media(album_first)
    await cache_incoming_topic_media(album_second)

    await send_public_reply(
        bot=bot,
        context=active_topic,
        message=make_message(text="/send", reply_to_message=album_second),
        command_args=None,
    )

    assert bot.download_calls == ["album-photo-1", "album-photo-2"]
    assert calls[0][0] == "TKT-100"
    assert calls[0][1] == "Album caption"
    assert len(calls[0][2]) == 2
    assert calls[0][2][0].content == b"img-1"
    assert calls[0][2][1].content == b"img-2"


@pytest.mark.asyncio
async def test_public_reply_obeys_allowlist(active_topic, monkeypatch):
    monkeypatch.setattr(config, "public_reply_ticket_allowlist", ("WHITELIST-ONLY",))

    with pytest.raises(OperatorReplyError):
        await send_public_reply(
            bot=DummyBot(),
            context=active_topic,
            message=make_message(text="/send Blocked reply"),
            command_args="Blocked reply",
        )


@pytest.mark.asyncio
async def test_public_reply_respects_feature_flag(active_topic, monkeypatch):
    monkeypatch.setattr(config, "public_reply_enabled", False)

    with pytest.raises(OperatorReplyError):
        await send_public_reply(
            bot=DummyBot(),
            context=active_topic,
            message=make_message(text="/send Blocked reply"),
            command_args="Blocked reply",
        )


@pytest.mark.asyncio
async def test_operator_context_rejects_unauthorized_user(initialized_db):
    await db_module.upsert_topic(
        "TKT-101",
        9002,
        unique_id="ABC-101",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="me",
        owner_name="Me",
        hde_link="https://hde.example.com/tickets/101",
    )

    with pytest.raises(OperatorReplyError):
        await get_operator_topic_context(
            telegram_user_id=999999,
            topic_id=9002,
        )


# ── sent_hde_messages DB tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_and_get_sent_message(initialized_db):
    await db_module.save_sent_message(
        telegram_message_id=111,
        topic_id=9001,
        ticket_id="TKT-100",
        hde_entity_id=42,
        entity_type="post",
    )
    record = await db_module.get_sent_message(111, 9001)
    assert record is not None
    assert record.hde_entity_id == 42
    assert record.entity_type == "post"
    assert record.ticket_id == "TKT-100"
    assert record.created_at


@pytest.mark.asyncio
async def test_get_sent_message_returns_none_when_missing(initialized_db):
    result = await db_module.get_sent_message(999, 9001)
    assert result is None


@pytest.mark.asyncio
async def test_delete_sent_message(initialized_db):
    await db_module.save_sent_message(111, 9001, "TKT-100", 42, "post")
    await db_module.delete_sent_message(111, 9001)
    result = await db_module.get_sent_message(111, 9001)
    assert result is None


@pytest.mark.asyncio
async def test_save_sent_message_replaces_existing(initialized_db):
    await db_module.save_sent_message(111, 9001, "TKT-100", 42, "post")
    await db_module.save_sent_message(111, 9001, "TKT-100", 99, "comment")
    record = await db_module.get_sent_message(111, 9001)
    assert record.hde_entity_id == 99
    assert record.entity_type == "comment"


@pytest.mark.asyncio
async def test_hde_api_update_post_calls_put(monkeypatch):
    calls = []

    async def fake_put(self, path, *, text=""):
        calls.append(("PUT", path, text))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient._put", fake_put)
    client = HDEApiClient()
    await client.update_post("TKT-100", 42, "updated text")
    assert calls == [("PUT", "/tickets/TKT-100/posts/42/", "updated text")]


@pytest.mark.asyncio
async def test_hde_api_delete_post_calls_delete(monkeypatch):
    calls = []

    async def fake_delete(self, path):
        calls.append(("DELETE", path))
        return object()

    monkeypatch.setattr("bot.hde_api.HDEApiClient._delete", fake_delete)
    client = HDEApiClient()
    await client.delete_post("TKT-100", 42)
    assert calls == [("DELETE", "/tickets/TKT-100/posts/42/")]


@pytest.mark.asyncio
async def test_edit_operator_message_updates_hde_post(active_topic, initialized_db, monkeypatch):
    await db_module.save_sent_message(
        telegram_message_id=501,
        topic_id=9001,
        ticket_id="TKT-100",
        hde_entity_id=77,
        entity_type="post",
    )
    calls = []

    async def fake_update_post(self, ticket_id, post_id, text):
        calls.append(("update_post", ticket_id, post_id, text))

    monkeypatch.setattr("bot.hde_api.HDEApiClient.update_post", fake_update_post)

    result = await edit_operator_message(
        context=active_topic,
        telegram_message_id=501,
        new_text="corrected text",
    )

    assert "обновлено" in result
    assert calls == [("update_post", "TKT-100", 77, "corrected text")]


@pytest.mark.asyncio
async def test_edit_operator_message_raises_if_not_found(active_topic, initialized_db):
    with pytest.raises(OperatorReplyError, match="не связано с HDE"):
        await edit_operator_message(
            context=active_topic,
            telegram_message_id=999,
            new_text="anything",
        )


@pytest.mark.asyncio
async def test_delete_operator_message_deletes_hde_post(active_topic, initialized_db, monkeypatch):
    await db_module.save_sent_message(
        telegram_message_id=502,
        topic_id=9001,
        ticket_id="TKT-100",
        hde_entity_id=88,
        entity_type="post",
    )
    calls = []

    async def fake_delete_post(self, ticket_id, post_id):
        calls.append(("delete_post", ticket_id, post_id))

    monkeypatch.setattr("bot.hde_api.HDEApiClient.delete_post", fake_delete_post)

    result = await delete_operator_message(
        context=active_topic,
        telegram_message_id=502,
    )

    assert "удалено" in result
    assert calls == [("delete_post", "TKT-100", 88)]
    assert await db_module.get_sent_message(502, 9001) is None


@pytest.mark.asyncio
async def test_delete_operator_message_raises_if_not_found(active_topic, initialized_db):
    with pytest.raises(OperatorReplyError, match="не связано с HDE"):
        await delete_operator_message(
            context=active_topic,
            telegram_message_id=999,
        )


# ── list_overnight_assigned + list_active_topics ───────────────────────────────

@pytest.mark.asyncio
async def test_list_overnight_assigned_returns_matching(initialized_db):
    await db_module.upsert_topic(
        "TKT-200", 9100,
        unique_id="ABC-200", company_name="ACME", ticket_name="Night ticket",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/200",
    )
    await db_module.update_topic("TKT-200", last_assigned_at="2026-04-04 02:00:00")

    results = await db_module.list_overnight_assigned("2026-04-03 15:00:00", "2026-04-04 05:00:00")
    assert any(r.ticket_id == "TKT-200" for r in results)


@pytest.mark.asyncio
async def test_list_overnight_assigned_excludes_outside_window(initialized_db):
    await db_module.upsert_topic(
        "TKT-201", 9101,
        unique_id="ABC-201", company_name="ACME", ticket_name="Day ticket",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/201",
    )
    await db_module.update_topic("TKT-201", last_assigned_at="2026-04-04 10:00:00")

    results = await db_module.list_overnight_assigned("2026-04-03 15:00:00", "2026-04-04 05:00:00")
    assert not any(r.ticket_id == "TKT-201" for r in results)


@pytest.mark.asyncio
async def test_list_active_topics_returns_active(initialized_db):
    await db_module.upsert_topic(
        "TKT-210", 9110,
        unique_id="ABC-210", company_name="ACME", ticket_name="Active",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/210",
    )
    results = await db_module.list_active_topics()
    assert any(r.ticket_id == "TKT-210" for r in results)


@pytest.mark.asyncio
async def test_list_active_topics_excludes_deleted(initialized_db):
    await db_module.upsert_topic(
        "TKT-211", 9111,
        unique_id="ABC-211", company_name="ACME", ticket_name="Deleted",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/211",
    )
    await db_module.update_topic("TKT-211", topic_state="deleted")
    results = await db_module.list_active_topics()
    assert not any(r.ticket_id == "TKT-211" for r in results)


@pytest.mark.asyncio
async def test_get_my_open_tickets_returns_list(monkeypatch):
    fake_response = {
        "data": {
            "10": {
                "id": 10,
                "unique_id": "ABC-010",
                "title": "Test ticket",
                "user_name": "John",
                "user_lastname": "Doe",
                "owner_id": 1,
                "sla_date": "05.04.2026 10:00",
            }
        },
        "meta": {"total_pages": 1},
    }

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}
        async def json(self):
            return fake_response
        async def text(self):
            import json
            return json.dumps(fake_response)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, url, params=None):
            return FakeResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("aiohttp.ClientSession", lambda **kwargs: FakeSession())
    client = HDEApiClient()
    tickets = await client.get_my_open_tickets()
    assert len(tickets) == 1
    assert tickets[0].unique_id == "ABC-010"
    assert tickets[0].title == "Test ticket"
    assert tickets[0].company_name == "John Doe"
    assert tickets[0].sla_date == "05.04.2026 10:00"


@pytest.mark.asyncio
async def test_send_morning_digest_sends_message(initialized_db, monkeypatch):
    async def fake_get_tickets(self):
        return []

    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", fake_get_tickets)

    from bot.digest import send_morning_digest
    bot = DummyBot()
    await send_morning_digest(bot=bot)

    assert len(bot.sent_messages) == 1
    chat_id, text, kwargs = bot.sent_messages[0]
    assert "Сводка за ночь" in text


@pytest.mark.asyncio
async def test_refresh_marks_stale_topic_deleted(initialized_db, monkeypatch):
    await db_module.upsert_topic(
        "TKT-300", 9200,
        unique_id="ABC-300", company_name="ACME", ticket_name="Stale",
        priority="medium", status="open", owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/300",
    )

    async def fake_get_tickets(self):
        return []

    monkeypatch.setattr("bot.hde_api.HDEApiClient.get_my_open_tickets", fake_get_tickets)

    close_calls = []

    async def fake_delete_topic(self, chat_id, message_thread_id):
        close_calls.append(message_thread_id)

    monkeypatch.setattr("aiogram.Bot.delete_forum_topic", fake_delete_topic)

    from bot.refresh import refresh_topics
    result = await refresh_topics(bot=DummyBot())

    assert len(result.marked_deleted) == 1
    assert result.marked_deleted[0].ticket_id == "TKT-300"
    assert result.active_after == 0

    topic = await db_module.get_topic("TKT-300")
    assert topic.is_deleted
