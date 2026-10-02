import pytest
from bot import db as db_module


@pytest.mark.asyncio
async def test_save_and_get_general_message(initialized_db):
    await db_module.save_general_message("TKT-1", message_id=999, ticket_name="Broken fan", chat_id=-100123456789)
    row = await db_module.get_general_message("TKT-1")
    assert row is not None
    assert row["message_id"] == 999
    assert row["ticket_name"] == "Broken fan"


@pytest.mark.asyncio
async def test_get_general_message_missing(initialized_db):
    row = await db_module.get_general_message("TKT-MISSING")
    assert row is None


@pytest.mark.asyncio
async def test_delete_general_message(initialized_db):
    await db_module.save_general_message("TKT-2", message_id=100, ticket_name="Old", chat_id=-100123456789)
    await db_module.delete_general_message("TKT-2")
    row = await db_module.get_general_message("TKT-2")
    assert row is None


@pytest.mark.asyncio
async def test_update_general_message_ticket_name(initialized_db):
    await db_module.save_general_message("TKT-3", message_id=200, ticket_name="Old name", chat_id=-100123456789)
    await db_module.save_general_message("TKT-3", message_id=200, ticket_name="New name", chat_id=-100123456789)
    row = await db_module.get_general_message("TKT-3")
    assert row["ticket_name"] == "New name"


from bot.general_channel import _is_unassigned, _format_general_message


def test_is_unassigned_empty_owner():
    assert _is_unassigned(owner_name="", department="Оборудование", target_dept="Оборудование") is True


def test_is_unassigned_explicit_label():
    assert _is_unassigned(owner_name="Неприсвоенный", department="Оборудование", target_dept="Оборудование") is True


def test_is_unassigned_wrong_department():
    assert _is_unassigned(owner_name="", department="Другой", target_dept="Оборудование") is False


def test_is_unassigned_no_department_filter():
    # When target_dept is empty, any department passes
    assert _is_unassigned(owner_name="", department="Anything", target_dept="") is True


def test_is_unassigned_has_owner():
    assert _is_unassigned(owner_name="Иван Петров", department="Оборудование", target_dept="Оборудование") is False


def test_format_general_message_contains_ticket():
    text = _format_general_message(
        display_id="А-12345",
        ticket_name="Сломался принтер",
        link="https://hde.example.com/tickets/1",
    )
    assert "А-12345" in text
    assert "Сломался принтер" in text
    assert "https://hde.example.com/tickets/1" in text
    assert "Неприсвоенный тикет" in text


@pytest.mark.asyncio
async def test_unassigned_ticket_is_announced_in_every_group(initialized_db, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from bot import general_channel, operators
    from bot.config import config
    monkeypatch.setattr(config, "general_topic_id", 1)
    monkeypatch.setattr("bot.work_schedule.is_work_time", lambda: True)
    monkeypatch.setattr(operators, "COLLEAGUES", (operators.Operator("102", "Максим Яницкий", 777, -100222),))
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    payload = {"ticket_id": "T9", "unique_id": "T9", "ticket_name": "Касса", "owner_name": "", "department": ""}

    await general_channel.on_assigned_on_create(bot, payload)
    await general_channel.on_assigned_on_create(bot, payload)  # a duplicate webhook posts nothing new

    sent_to = [c.kwargs["chat_id"] for c in bot.send_message.call_args_list]
    assert sent_to == [config.group_chat_id, -100222]
    await general_channel.on_ticket_closed(bot, payload)
    deleted_in = sorted(c.kwargs["chat_id"] for c in bot.delete_message.call_args_list)
    assert deleted_in == sorted([config.group_chat_id, -100222])
    assert await db_module.list_general_messages_for("T9") == []
