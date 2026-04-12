from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.db as db_module
import bot.topic_manager as topic_manager
from bot.scheduler import process_scheduled_actions
from bot.time_utils import parse_datetime, to_storage, utcnow
from bot.topic_manager import (
    handle_assigned_on_create,
    handle_client_reply,
    handle_owner_changed,
    handle_staff_reply,
    handle_ticket_closed,
    handle_ticket_updated,
)


def make_payload(**overrides):
    payload = {
        "ticket_id": "TKT-1",
        "unique_id": "ABC-123",
        "ticket_name": "Broken printer",
        "company_name": "ACME",
        "priority": "high",
        "status": "open",
        "owner_id": "me",
        "owner_name": "Me",
        "user_name": "Alice",
        "message": "Need help",
        "last_post_date": "2026-04-03 12:00:00",
        "sla_remaining_minutes": "30",
        "link": "https://hde.example.com/tickets/1",
    }
    payload.update(overrides)
    return payload


def make_bot():
    bot = AsyncMock()
    forum_topic = MagicMock()
    forum_topic.message_thread_id = 999
    bot.create_forum_topic = AsyncMock(return_value=forum_topic)
    bot.edit_forum_topic = AsyncMock()
    bot.reopen_forum_topic = AsyncMock()
    bot.close_forum_topic = AsyncMock()
    bot.delete_forum_topic = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_video = AsyncMock()
    bot.send_voice = AsyncMock()
    bot.send_audio = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_media_group = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_assigned_on_create_creates_topic(initialized_db, monkeypatch):
    monkeypatch.setattr(topic_manager, "_is_work_time", lambda: True)
    bot = make_bot()

    await handle_assigned_on_create(bot, make_payload())

    bot.create_forum_topic.assert_called_once()
    bot.send_message.assert_called_once()

    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.is_active is True


@pytest.mark.asyncio
async def test_owner_changed_creates_topic(initialized_db, monkeypatch):
    monkeypatch.setattr(topic_manager, "_is_work_time", lambda: True)
    bot = make_bot()

    await handle_owner_changed(bot, make_payload())

    bot.create_forum_topic.assert_called_once()
    bot.send_message.assert_called_once()

    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.topic_id == 999
    assert record.is_active is True
    assert record.unique_id == "ABC-123"


@pytest.mark.asyncio
async def test_owner_changed_from_me_marks_topic_pending_delete(initialized_db):
    await db_module.upsert_topic(
        "TKT-1",
        999,
        unique_id="ABC-123",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="me",
        owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
    )
    bot = make_bot()

    await handle_owner_changed(bot, make_payload(owner_id="other", owner_name="Other"))

    bot.close_forum_topic.assert_called_once()
    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.is_pending_delete is True
    assert record.delete_after_at is not None
    assert record.pre_sla_notify_at is None


@pytest.mark.asyncio
async def test_owner_changed_back_to_me_reopens_existing_topic(initialized_db):
    await db_module.upsert_topic(
        "TKT-1",
        999,
        unique_id="ABC-123",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="me",
        owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
    )
    bot = make_bot()

    await handle_owner_changed(bot, make_payload(owner_id="other", owner_name="Other"))
    await handle_owner_changed(bot, make_payload(owner_id="me", owner_name="Me"))

    bot.close_forum_topic.assert_called_once()
    bot.reopen_forum_topic.assert_called_once()
    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.is_active is True
    assert record.topic_id == 999
    assert record.delete_after_at is None


@pytest.mark.asyncio
async def test_ticket_updated_renames_topic(initialized_db):
    await db_module.upsert_topic(
        "TKT-1",
        999,
        unique_id="ABC-123",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="me",
        owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
    )
    bot = make_bot()

    await handle_ticket_updated(bot, make_payload(ticket_name="Printer does not print"))

    bot.edit_forum_topic.assert_called_once()
    bot.send_message.assert_called_once()
    record = await db_module.get_topic("TKT-1")
    assert record.ticket_name == "Printer does not print"


@pytest.mark.asyncio
async def test_client_reply_creates_topic_and_schedules_pre_sla(initialized_db, monkeypatch):
    monkeypatch.setattr(topic_manager, "_is_work_time", lambda: True)
    bot = make_bot()

    await handle_client_reply(bot, make_payload())

    bot.create_forum_topic.assert_called_once()
    bot.send_message.assert_called_once()

    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.pre_sla_notify_at is not None
    notify_at = parse_datetime(record.pre_sla_notify_at)
    assert notify_at is not None


@pytest.mark.asyncio
async def test_client_reply_sends_photo_attachment(initialized_db, monkeypatch):
    monkeypatch.setattr(topic_manager, "_is_work_time", lambda: True)
    bot = make_bot()

    async def fake_download(ref):
        from bot.hde_api import HDEAttachment

        return HDEAttachment(
            filename="photo.jpg",
            content=b"image",
            content_type="image/jpeg",
        )

    monkeypatch.setattr(topic_manager, "download_client_attachment", fake_download)

    await handle_client_reply(
        bot,
        make_payload(
            attachments=[MagicMock(url="https://files.example.com/photo.jpg", filename="photo.jpg", content_type="image/jpeg")]
        ),
    )

    bot.send_photo.assert_called_once()


@pytest.mark.asyncio
async def test_client_reply_sends_voice_attachment(initialized_db, monkeypatch):
    monkeypatch.setattr(topic_manager, "_is_work_time", lambda: True)
    bot = make_bot()

    async def fake_download(ref):
        from bot.hde_api import HDEAttachment

        return HDEAttachment(
            filename="voice.ogg",
            content=b"voice",
            content_type="audio/ogg",
        )

    monkeypatch.setattr(topic_manager, "download_client_attachment", fake_download)

    await handle_client_reply(
        bot,
        make_payload(
            attachments=[MagicMock(url="https://files.example.com/voice.ogg", filename="voice.ogg", content_type="audio/ogg")]
        ),
    )

    bot.send_voice.assert_called_once()


@pytest.mark.asyncio
async def test_staff_reply_clears_pre_sla(initialized_db):
    await db_module.upsert_topic(
        "TKT-1",
        999,
        unique_id="ABC-123",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="me",
        owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=to_storage(utcnow() + timedelta(minutes=10)),
    )
    bot = make_bot()

    await handle_staff_reply(bot, make_payload(user_name="Support"))

    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.pre_sla_notify_at is None
    assert record.pre_sla_sent_at is None
    assert record.last_staff_reply_at is not None


@pytest.mark.asyncio
async def test_ticket_closed_deletes_topic(initialized_db):
    await db_module.upsert_topic(
        "TKT-1",
        999,
        unique_id="ABC-123",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="me",
        owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
    )
    bot = make_bot()

    await handle_ticket_closed(bot, make_payload(status="closed"))

    bot.delete_forum_topic.assert_called_once()
    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.is_deleted is True


@pytest.mark.asyncio
async def test_scheduler_sends_pre_sla_alert(initialized_db, monkeypatch):
    import bot.work_schedule as work_schedule
    monkeypatch.setattr(work_schedule, "is_work_time", lambda: True)
    monkeypatch.setattr(work_schedule, "is_work_day", lambda: True)
    monkeypatch.setattr(work_schedule, "was_yesterday_work_day", lambda: False)
    await db_module.upsert_topic(
        "TKT-1",
        999,
        unique_id="ABC-123",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="me",
        owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=to_storage(utcnow() - timedelta(minutes=1)),
    )
    bot = make_bot()

    await process_scheduled_actions(bot)

    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.pre_sla_sent_at is not None
    assert bot.send_message.called


@pytest.mark.asyncio
async def test_scheduler_deletes_pending_topics(initialized_db, monkeypatch):
    import bot.work_schedule as work_schedule
    monkeypatch.setattr(work_schedule, "is_work_time", lambda: True)
    monkeypatch.setattr(work_schedule, "is_work_day", lambda: True)
    monkeypatch.setattr(work_schedule, "was_yesterday_work_day", lambda: False)
    await db_module.upsert_topic(
        "TKT-1",
        999,
        unique_id="ABC-123",
        company_name="ACME",
        ticket_name="Broken printer",
        priority="high",
        status="open",
        owner_id="other",
        owner_name="Other",
        hde_link="https://hde.example.com/tickets/1",
        topic_state="pending_delete",
        delete_after_at=to_storage(utcnow() - timedelta(minutes=1)),
    )
    bot = make_bot()

    await process_scheduled_actions(bot)

    bot.delete_forum_topic.assert_called_once()
    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.is_deleted is True
