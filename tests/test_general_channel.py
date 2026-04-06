import pytest
from bot import db as db_module


@pytest.mark.asyncio
async def test_save_and_get_general_message(initialized_db):
    await db_module.save_general_message("TKT-1", message_id=999, ticket_name="Broken fan")
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
    await db_module.save_general_message("TKT-2", message_id=100, ticket_name="Old")
    await db_module.delete_general_message("TKT-2")
    row = await db_module.get_general_message("TKT-2")
    assert row is None


@pytest.mark.asyncio
async def test_update_general_message_ticket_name(initialized_db):
    await db_module.save_general_message("TKT-3", message_id=200, ticket_name="Old name")
    await db_module.save_general_message("TKT-3", message_id=200, ticket_name="New name")
    row = await db_module.get_general_message("TKT-3")
    assert row["ticket_name"] == "New name"
