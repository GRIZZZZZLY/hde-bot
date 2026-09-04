import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
    bot.send_message.return_value.message_id = 1001
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
async def test_client_reply_message_has_suggest_button(initialized_db, monkeypatch):
    """Phase 3: под сообщением клиента — кнопка «💡 Предложить ответ»."""
    monkeypatch.setattr(topic_manager, "_is_work_time", lambda: True)
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
    await db_module.update_topic("TKT-1", topic_state="active")
    bot = make_bot()

    await handle_client_reply(bot, make_payload())

    kb = bot.send_message.call_args.kwargs.get("reply_markup")
    assert kb is not None
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert any(b.callback_data == "ai:suggest" for b in buttons)


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
async def test_staff_reply_clears_pre_sla(initialized_db, monkeypatch):
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", False)
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
async def test_staff_reply_keeps_pre_sla_when_client_reply_is_newer(initialized_db):
    """Regression: a staff_reply older than the last client reply must NOT
    wipe the pre-SLA timer. Observed in prod: a burst of staff_reply events
    (staff_reply_at <= last_client_reply_at) kept clearing the freshly
    scheduled timer, so unanswered tickets never got a pre-SLA alert.
    """
    future_notify = to_storage(utcnow() + timedelta(minutes=10))
    client_reply_at = to_storage(utcnow())  # 2026 — newer than payload date
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
        pre_sla_notify_at=future_notify,
        last_client_reply_at=client_reply_at,
    )
    bot = make_bot()

    # make_payload default last_post_date is 2026-04-03 — OLDER than the
    # client reply just recorded, so this staff_reply must not clear pre-SLA.
    await handle_staff_reply(bot, make_payload(user_name="Support"))

    record = await db_module.get_topic("TKT-1")
    assert record is not None
    assert record.pre_sla_notify_at == future_notify  # preserved
    assert record.last_staff_reply_at is not None       # other updates still applied


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
async def test_staff_reply_same_second_does_not_clear_pre_sla(initialized_db, monkeypatch):
    """Regression (ticket 167908): when HDE's dispatcher emits a staff_reply
    in the same second as the client's post (or payload last_post_date is
    empty so utcnow() fallback kicks in with microseconds), the resulting
    sub-second drift made `reply_at > last_client` true even though the
    staff event is not genuinely newer. Same-second staff_reply must NOT
    clear an armed pre-SLA timer.
    """
    from datetime import datetime, timezone as _tz
    lcr = "2026-05-20 11:14:19"
    notify = "2026-05-20 11:24:19"
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=notify,
        last_client_reply_at=lcr,
    )
    # Force the staff_reply utcnow() fallback to drift sub-second past lcr
    fake_now = datetime(2026, 5, 20, 11, 14, 19, 500000, tzinfo=_tz.utc)
    monkeypatch.setattr(topic_manager, "utcnow", lambda: fake_now)
    bot = make_bot()
    # Empty last_post_date → parse_datetime returns None → utcnow() fallback
    await handle_staff_reply(bot, make_payload(last_post_date="", user_name="Support"))
    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at == notify  # timer preserved


@pytest.mark.asyncio
async def test_staff_reply_autoreply_does_not_clear_pre_sla(initialized_db, monkeypatch):
    """Automated staff_reply (PosifloraSupportBot / dispatcher echo) arrives
    newer than the client message but is NOT the operator. With HDE verify on,
    the timer must survive so pre-SLA can still fire (ticket 171623)."""
    notify = to_storage(utcnow() + timedelta(minutes=10))
    lcr = to_storage(utcnow() - timedelta(minutes=1))
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=notify, last_client_reply_at=lcr,
    )
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "me")
    verify = AsyncMock(return_value=False)  # operator has NOT posted in HDE
    monkeypatch.setattr(topic_manager, "_hde_staff_replied_since", verify)
    bot = make_bot()

    await handle_staff_reply(bot, make_payload(
        last_post_date=to_storage(utcnow()), user_name="PosifloraSupportBot"))

    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at == notify   # timer preserved
    verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_staff_reply_operator_reply_clears_pre_sla(initialized_db, monkeypatch):
    """A genuine operator reply (verified present in HDE) clears the timer."""
    notify = to_storage(utcnow() + timedelta(minutes=10))
    lcr = to_storage(utcnow() - timedelta(minutes=1))
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=notify, last_client_reply_at=lcr,
    )
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "me")
    verify = AsyncMock(return_value=True)  # operator genuinely replied
    monkeypatch.setattr(topic_manager, "_hde_staff_replied_since", verify)
    bot = make_bot()

    await handle_staff_reply(bot, make_payload(
        last_post_date=to_storage(utcnow()), user_name="Me"))

    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at is None     # timer cleared
    verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_staff_reply_legacy_clear_when_verify_disabled(initialized_db, monkeypatch):
    """With HDE verify disabled, fall back to the timestamp comparison: a
    staff_reply newer than the client clears the timer and makes no API call."""
    notify = to_storage(utcnow() + timedelta(minutes=10))
    lcr = to_storage(utcnow() - timedelta(minutes=1))
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=notify, last_client_reply_at=lcr,
    )
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", False)
    verify = AsyncMock(return_value=False)
    monkeypatch.setattr(topic_manager, "_hde_staff_replied_since", verify)
    bot = make_bot()

    await handle_staff_reply(bot, make_payload(last_post_date=to_storage(utcnow())))

    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at is None     # cleared (legacy timestamp path)
    verify.assert_not_awaited()              # no HDE API call


def _msk_stamp(dt):
    """HDE post date_created format ("HH:MM:SS DD.MM.YYYY", MSK)."""
    from datetime import timezone as _tz
    return (dt + timedelta(hours=3)).replace(tzinfo=None).strftime("%H:%M:%S %d.%m.%Y")


def _hde_post(user_id, text, dt, post_id=1):
    from bot.hde_api import HDEPost
    return HDEPost(
        post_id=post_id,
        user_id=user_id,
        text=text,
        date_created=_msk_stamp(dt),
    )


@pytest.mark.asyncio
async def test_staff_replied_counts_colleague_from_static_list(monkeypatch):
    """Любой сотрудник поддержки, а не только HDE_OWNER_ID, считается ответом.
    Прод, тикет 200484: коллега 46292 ответил клиенту в 14:31:01, а через 9
    секунд бот прислал клиенту «занимаюсь вашим вопросом» — проверка видела
    только оператора 98 и считала тикет неотвеченным."""
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "98")
    monkeypatch.setattr(topic_manager.config, "agent_staff_user_ids", ("98", "46292"))
    topic_manager._STAFF_GROUP_CACHE.clear()

    since = utcnow() - timedelta(minutes=18)
    client = MagicMock()
    client.get_ticket_posts = AsyncMock(
        return_value=[_hde_post(46292, "<p>Чем ещё помочь?</p>", utcnow())]
    )
    client.get_user_group_type = AsyncMock()

    with patch("bot.hde_api.HDEApiClient", return_value=client):
        replied = await topic_manager._hde_staff_replied_since("TKT-1", to_storage(since))

    assert replied is True
    client.get_user_group_type.assert_not_awaited()  # список покрыл — API не нужен


@pytest.mark.asyncio
async def test_staff_replied_resolves_unknown_author_via_api(monkeypatch):
    """Автора, которого нет в статическом списке, добираем через
    GET /users/{id} → group.type. Результат кэшируется: второй проход по тому
    же id не идёт в API (у HDE 300 req/min и общий бан на 20 минут)."""
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "98")
    monkeypatch.setattr(topic_manager.config, "agent_staff_user_ids", ("98",))
    topic_manager._STAFF_GROUP_CACHE.clear()

    since = utcnow() - timedelta(minutes=5)
    posts = [_hde_post(51281, "<p>Обращение принято</p>", utcnow())]
    client = MagicMock()
    client.get_ticket_posts = AsyncMock(return_value=posts)
    client.get_user_group_type = AsyncMock(return_value="staff")

    with patch("bot.hde_api.HDEApiClient", return_value=client):
        first = await topic_manager._hde_staff_replied_since("TKT-1", to_storage(since))
        second = await topic_manager._hde_staff_replied_since("TKT-1", to_storage(since))

    assert first is True and second is True
    assert client.get_user_group_type.await_count == 1  # второй раз из кэша


@pytest.mark.asyncio
async def test_staff_replied_ignores_client_author(monkeypatch):
    """Пост клиента ответом сотрудника не считается. group.type у клиента на
    проде — 'user' (замер 2026-09-04 на uid 37756), у сотрудника 'staff'."""
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "98")
    monkeypatch.setattr(topic_manager.config, "agent_staff_user_ids", ("98",))
    topic_manager._STAFF_GROUP_CACHE.clear()

    since = utcnow() - timedelta(minutes=5)
    client = MagicMock()
    client.get_ticket_posts = AsyncMock(
        return_value=[_hde_post(37756, "приняли", utcnow())]
    )
    client.get_user_group_type = AsyncMock(return_value="user")

    with patch("bot.hde_api.HDEApiClient", return_value=client):
        replied = await topic_manager._hde_staff_replied_since("TKT-1", to_storage(since))

    assert replied is False


@pytest.mark.asyncio
async def test_staff_replied_ignores_own_autoreply(monkeypatch):
    """Бот постит автоответ через HDE-аккаунт оператора (прод: uid 98 ==
    HDE_OWNER_ID), поэтому свой же автоответ проходил как «оператор ответил»
    и гасил счётчик, хотя человек не отвечал. Пост с текстом
    REASSURANCE_TEXT не считается ответом."""
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "98")
    monkeypatch.setattr(topic_manager.config, "agent_staff_user_ids", ("98",))
    monkeypatch.setattr(
        topic_manager.config, "reassurance_text",
        "Я про вас не забыл, занимаюсь вашим вопросом",
    )
    topic_manager._STAFF_GROUP_CACHE.clear()

    since = utcnow() - timedelta(minutes=18)
    client = MagicMock()
    client.get_ticket_posts = AsyncMock(return_value=[
        _hde_post(98, "Я про вас не забыл, занимаюсь вашим вопросом", utcnow()),
    ])
    client.get_user_group_type = AsyncMock()

    with patch("bot.hde_api.HDEApiClient", return_value=client):
        replied = await topic_manager._hde_staff_replied_since("TKT-1", to_storage(since))

    assert replied is False


@pytest.mark.asyncio
async def test_schedule_pre_sla_resets_reassurance_flag(initialized_db):
    """Перевзвод таймера обнуляет reassurance_sent_at: иначе автоответ по
    тикету физически возможен только один раз за его жизнь."""
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
    )
    await db_module.update_topic("TKT-1", reassurance_sent_at=to_storage(utcnow()))

    reply_at = to_storage(utcnow())
    await topic_manager._schedule_pre_sla("TKT-1", make_payload(), reply_at)

    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at is not None
    assert rec.reassurance_sent_at is None


@pytest.mark.asyncio
async def test_staff_reply_clear_resets_reassurance_flag(initialized_db, monkeypatch):
    """Очистка таймера по ответу сотрудника снимает и флаг автоответа."""
    notify = to_storage(utcnow() + timedelta(minutes=10))
    lcr = to_storage(utcnow() - timedelta(minutes=1))
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        pre_sla_notify_at=notify, last_client_reply_at=lcr,
    )
    await db_module.update_topic("TKT-1", reassurance_sent_at=to_storage(utcnow()))
    monkeypatch.setattr(topic_manager.config, "presla_hde_verify", True)
    monkeypatch.setattr(topic_manager.config, "hde_owner_id", "98")
    monkeypatch.setattr(
        topic_manager, "_hde_staff_replied_since", AsyncMock(return_value=True)
    )
    bot = make_bot()

    await handle_staff_reply(bot, make_payload(last_post_date=to_storage(utcnow())))

    rec = await db_module.get_topic("TKT-1")
    assert rec.pre_sla_notify_at is None
    assert rec.reassurance_sent_at is None


@pytest.mark.asyncio
async def test_ticket_closed_serialized_on_ticket_lock(initialized_db):
    """Regression: handle_ticket_closed must serialize on the shared
    per-ticket lock. Prod incident (ticket 167924): a slow in-flight
    owner_changed (holding _ticket_lock during a ~30s AI-summary) ran
    concurrently with an unlocked ticket_closed; closure marked the
    topic deleted, then the in-flight _ensure_active_topic re-read the
    deleted record and RESURRECTED a fresh, never-deleted topic.
    """
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
    )
    bot = make_bot()
    lock = topic_manager._ticket_lock("TKT-1")
    await lock.acquire()
    task = asyncio.create_task(
        handle_ticket_closed(bot, make_payload(status="closed"))
    )
    try:
        await asyncio.sleep(0.05)
        # Blocked on the shared per-ticket lock → not yet deleted
        rec = await db_module.get_topic("TKT-1")
        assert rec is not None and rec.is_deleted is False
        bot.delete_forum_topic.assert_not_called()
    finally:
        lock.release()
    await task
    rec = await db_module.get_topic("TKT-1")
    assert rec.is_deleted is True
    bot.delete_forum_topic.assert_called_once()


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


@pytest.mark.asyncio
async def test_scheduler_hde_verify_called_once_per_pass(initialized_db, monkeypatch):
    """One scheduler pass must hit HDE at most once per ticket: the countdown
    loop and the reassurance loop share a single _hde_staff_replied_since
    result. The cache must NOT survive into the next pass."""
    import bot.scheduler as scheduler
    import bot.work_schedule as work_schedule
    monkeypatch.setattr(work_schedule, "is_work_time", lambda: True)
    monkeypatch.setattr(work_schedule, "is_work_day", lambda: True)
    monkeypatch.setattr(work_schedule, "was_yesterday_work_day", lambda: False)
    # Active countdown (sent 2 min ago, >55s throttle) that is also inside the
    # reassurance window: notify_at 9 min ago + 10 min warning ⇒ deadline in
    # 1 min ≤ reassurance_minutes_before (2).
    await db_module.upsert_topic(
        "TKT-1", 999, unique_id="ABC-123", company_name="ACME",
        ticket_name="Broken printer", priority="high", status="open",
        owner_id="me", owner_name="Me",
        hde_link="https://hde.example.com/tickets/1",
        last_client_reply_at=to_storage(utcnow() - timedelta(minutes=20)),
        pre_sla_notify_at=to_storage(utcnow() - timedelta(minutes=9)),
        pre_sla_sent_at=to_storage(utcnow() - timedelta(minutes=2)),
    )
    await db_module.update_topic("TKT-1", pre_sla_message_id=555)
    verify = AsyncMock(return_value=False)
    monkeypatch.setattr(scheduler, "_hde_staff_replied_since", verify)
    monkeypatch.setattr(scheduler, "update_pre_sla_alert", AsyncMock())
    monkeypatch.setattr(scheduler, "send_reassurance_to_client", AsyncMock())
    bot = make_bot()

    await process_scheduled_actions(bot)
    assert verify.await_count == 1  # countdown + reassurance share one check

    await process_scheduled_actions(bot)
    assert verify.await_count == 2  # next pass re-verifies (no cross-pass cache)


@pytest.mark.asyncio
async def test_post_ticket_history_autofill_called(monkeypatch):
    """apply_ticket_fields is awaited once after a successful AI summary."""
    from bot.hde_api import HDEPost, HDETicketInfo

    bot = make_bot()

    fake_info = HDETicketInfo(
        client_id=1, client_name="Alice", owner_id=2, owner_name="Bob"
    )
    fake_post = HDEPost(
        post_id=1,
        user_id=1,
        text="Hello",
        date_created="10:00:00 01.01.2026",
        is_comment=False,
    )

    # patch has_hde_api_credentials on the singleton used inside topic_manager
    monkeypatch.setattr(
        topic_manager.config, "has_hde_api_credentials", lambda: True
    )

    apply_mock = AsyncMock()

    with (
        patch("bot.hde_api.HDEApiClient") as MockClient,
        patch("bot.topic_manager._post_client_history", new_callable=AsyncMock),
        patch("bot.topic_manager._generate_summary_with_retry", new_callable=AsyncMock) as mock_gen,
        patch("bot.topic_manager.format_ticket_history", return_value=[]),
        patch("bot.handlers.ai_feedback.suit_feedback_kb", return_value=None),
        patch("bot.handlers.ai_feedback.answer_feedback_kb", return_value=None),
        patch("bot.handlers.ai_feedback.memo_feedback_kb", return_value=None),
        patch("bot.handlers.ai_feedback.register_feedback_pending", new_callable=AsyncMock),
        patch("bot.topic_manager.db") as mock_db,
        patch("bot.ticket_fields.apply_ticket_fields", apply_mock),
    ):
        instance = MockClient.return_value
        instance.get_ticket_info = AsyncMock(return_value=fake_info)
        instance.get_ticket_posts = AsyncMock(return_value=[fake_post])
        instance.get_ticket_comments = AsyncMock(return_value=[])
        mock_gen.return_value = ("суть", "клиенту", "памятка", 80)
        mock_db.update_topic = AsyncMock()

        await topic_manager._post_ticket_history(bot, "TKT-9", 999, ticket_title="T", company_id="")

    apply_mock.assert_awaited_once()
    call_args = apply_mock.await_args.args
    assert call_args[0] is bot
    assert call_args[1] == "TKT-9"
    assert call_args[2] == 999
    assert isinstance(call_args[3], str)


@pytest.mark.asyncio
async def test_post_ticket_history_reuses_hde_client_for_client_history(monkeypatch):
    from bot.hde_api import HDETicketInfo

    bot = make_bot()
    instances = []

    class FakeClient:
        def __init__(self):
            self.ticket_info_calls = 0
            self.client_tickets_calls = 0
            instances.append(self)

        async def get_ticket_info(self, ticket_id):
            self.ticket_info_calls += 1
            return HDETicketInfo(
                client_id=1,
                client_name="Alice",
                owner_id=2,
                owner_name="Bob",
            )

        async def get_ticket_posts(self, ticket_id):
            return []

        async def get_ticket_comments(self, ticket_id):
            return []

        async def get_client_tickets(self, client_id, limit=10):
            self.client_tickets_calls += 1
            return []

    monkeypatch.setattr(topic_manager.config, "has_hde_api_credentials", lambda: True)
    monkeypatch.setattr("bot.hde_api.HDEApiClient", FakeClient)

    await topic_manager._post_ticket_history(bot, "TKT-9", 999, ticket_title="T")

    assert len(instances) == 1
    assert instances[0].ticket_info_calls == 1
    assert instances[0].client_tickets_calls == 1


@pytest.mark.asyncio
async def test_retry_missing_ai_summaries_reuses_one_hde_client(monkeypatch):
    from bot.hde_api import HDEPost, HDETicketInfo

    bot = make_bot()
    instances = []

    class FakeClient:
        def __init__(self):
            instances.append(self)

        async def get_ticket_info(self, ticket_id):
            return HDETicketInfo(
                client_id=1,
                client_name="Alice",
                owner_id=2,
                owner_name="Bob",
            )

        async def get_ticket_posts(self, ticket_id):
            return [
                HDEPost(
                    post_id=1,
                    user_id=1,
                    text=f"Message {ticket_id}",
                    date_created="10:00:00 01.01.2026",
                )
            ]

        async def get_ticket_comments(self, ticket_id):
            return []

    records = [
        db_module.TicketTopic(
            ticket_id="TKT-1",
            unique_id="ABC-1",
            ticket_name="Ticket 1",
            company_name="ACME",
            topic_id=101,
            topic_state="active",
            priority="medium",
            status="open",
            owner_id="me",
            owner_name="Me",
            delete_after_at=None,
            last_client_reply_at=None,
            last_staff_reply_at=None,
            pre_sla_notify_at=None,
            pre_sla_sent_at=None,
            pre_sla_message_id=None,
            reassurance_sent_at=None,
            hde_link="https://hde.example.com/tickets/1",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
            deleted_at=None,
            last_assigned_at=None,
            ai_summary_sent_at=None,
        ),
        db_module.TicketTopic(
            ticket_id="TKT-2",
            unique_id="ABC-2",
            ticket_name="Ticket 2",
            company_name="ACME",
            topic_id=102,
            topic_state="active",
            priority="medium",
            status="open",
            owner_id="me",
            owner_name="Me",
            delete_after_at=None,
            last_client_reply_at=None,
            last_staff_reply_at=None,
            pre_sla_notify_at=None,
            pre_sla_sent_at=None,
            pre_sla_message_id=None,
            reassurance_sent_at=None,
            hde_link="https://hde.example.com/tickets/2",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
            deleted_at=None,
            last_assigned_at=None,
            ai_summary_sent_at=None,
        ),
    ]

    monkeypatch.setattr(topic_manager.db, "list_topics_missing_summary", AsyncMock(return_value=records))
    monkeypatch.setattr(topic_manager.db, "update_topic", AsyncMock())
    monkeypatch.setattr("bot.hde_api.HDEApiClient", FakeClient)
    monkeypatch.setattr("bot.handlers.ai_feedback.register_feedback_pending", AsyncMock())
    monkeypatch.setattr(
        topic_manager,
        "_generate_summary_with_retry",
        AsyncMock(return_value=("Суть", "Ответ", "Памятка", 80)),
    )

    sent = await topic_manager.retry_missing_ai_summaries(bot)

    assert sent == 2
    assert len(instances) == 1
