"""Each engineer's own working hours: the primary engineer off duty must not mute a colleague."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import db, general_channel, operators, topic_manager, work_schedule
from bot.config import config

ALWAYS = dict(work_days=tuple(range(7)), work_hours=(0, 24))
NEVER = dict(work_days=tuple(range(7)), work_hours=(0, 0))


def _maxim(**schedule):
    return operators.Operator("102", "Максим Яницкий", 1220214456, -100222, **schedule)


def test_own_schedule_wins_and_empty_schedule_follows_env(monkeypatch):
    monkeypatch.setattr(work_schedule, "is_work_time", lambda: False)  # primary engineer off
    assert work_schedule.is_work_time_for(_maxim(**ALWAYS)) is True
    assert work_schedule.is_work_time_for(_maxim(**NEVER)) is False
    assert work_schedule.is_work_time_for(_maxim()) is False  # no own schedule → .env schedule
    assert work_schedule.is_work_time_for(None) is False


@pytest.mark.asyncio
async def test_colleagues_client_reply_arrives_while_primary_is_off(initialized_db, monkeypatch):
    monkeypatch.setattr(work_schedule, "is_work_time", lambda: False)
    monkeypatch.setattr(operators, "COLLEAGUES", (_maxim(**ALWAYS),))
    monkeypatch.setattr(topic_manager.config, "agent_voice_v2_enabled", False)
    await db.upsert_topic("TKT-1", 7, chat_id=-100222, owner_id="102", owner_name="Максим Яницкий")
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))

    await topic_manager.handle_client_reply(bot, {
        "ticket_id": "TKT-1", "unique_id": "TKT-1", "owner_id": "", "owner_name": "",
        "user_name": "Клиент", "message": "Касса не печатает", "ticket_name": "Касса",
    })

    assert any(c.kwargs.get("chat_id") == -100222 for c in bot.send_message.call_args_list)


@pytest.mark.asyncio
async def test_primary_off_mutes_only_the_primary_group(initialized_db, monkeypatch):
    monkeypatch.setattr(work_schedule, "is_work_time", lambda: False)
    await db.upsert_topic("TKT-2", 9, chat_id=config.group_chat_id, owner_id="me", owner_name="Me")
    bot = AsyncMock()

    await topic_manager.handle_client_reply(bot, {
        "ticket_id": "TKT-2", "owner_id": "me", "owner_name": "Me", "message": "?",
    })

    bot.send_message.assert_not_called()  # unchanged behaviour for the primary engineer


@pytest.mark.asyncio
async def test_general_goes_only_to_groups_at_work(initialized_db, monkeypatch):
    monkeypatch.setattr(config, "general_topic_id", 1)
    monkeypatch.setattr(work_schedule, "is_work_time", lambda: False)
    monkeypatch.setattr(operators, "COLLEAGUES", (_maxim(**ALWAYS),))
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))

    await general_channel.on_assigned_on_create(bot, {
        "ticket_id": "T9", "unique_id": "T9", "ticket_name": "тест", "owner_name": "", "department": "",
    })

    assert [c.kwargs["chat_id"] for c in bot.send_message.call_args_list] == [-100222]
    assert [r["chat_id"] for r in await db.list_general_messages_for("T9")] == [-100222]
