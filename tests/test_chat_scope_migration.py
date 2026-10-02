"""Start-up migration: a single-group database becomes chat-scoped without losing rows."""
import aiosqlite
import pytest

from bot import db
from bot.config import config

_OLD_SCHEMA = [
    """CREATE TABLE ticket_topics (
        ticket_id TEXT PRIMARY KEY, topic_id INTEGER NOT NULL, company TEXT DEFAULT '',
        ticket_name TEXT DEFAULT '', is_closed INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')), closed_at TEXT)""",
    """CREATE TABLE topic_media_cache (
        topic_id INTEGER NOT NULL, message_id INTEGER NOT NULL, media_group_id TEXT,
        attachment_kind TEXT NOT NULL, file_id TEXT NOT NULL, filename TEXT DEFAULT '',
        content_type TEXT DEFAULT '', text TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (topic_id, message_id))""",
    "CREATE INDEX idx_topic_media_group ON topic_media_cache(topic_id, media_group_id, message_id)",
    """CREATE TABLE sent_hde_messages (
        telegram_message_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, ticket_id TEXT NOT NULL,
        hde_entity_id INTEGER NOT NULL, entity_type TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (telegram_message_id, topic_id))""",
    # very old shape: no answer_text / ai_full_text yet
    """CREATE TABLE ai_feedback_pending (
        topic_id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL, history TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL)""",
    """CREATE TABLE unassigned_general_messages (
        ticket_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL, ticket_name TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""",
    "INSERT INTO unassigned_general_messages (ticket_id, message_id, ticket_name) VALUES ('T2', 70, 'Принтер')",
    "INSERT INTO ticket_topics (ticket_id, topic_id, ticket_name) VALUES ('T1', 350, 'Касса')",
    "INSERT INTO topic_media_cache (topic_id, message_id, attachment_kind, file_id) VALUES (350, 7, 'photo', 'F')",
    "INSERT INTO sent_hde_messages VALUES (11, 350, 'T1', 900, 'post', datetime('now'))",
    "INSERT INTO ai_feedback_pending VALUES (350, 'T1', 'h', 't', '2999-01-01T00:00:00+00:00')",
]


@pytest.mark.asyncio
async def test_old_database_is_scoped_to_the_primary_group(set_test_db):
    async with aiosqlite.connect(set_test_db) as conn:
        for sql in _OLD_SCHEMA:
            await conn.execute(sql)
        await conn.commit()

    await db.init_db()
    await db.init_db()  # a second start is a no-op

    group = config.group_chat_id
    topic = await db.get_topic_by_topic_id(group, 350)
    assert topic is not None and topic.ticket_id == "T1" and topic.chat_id == group
    assert await db.get_topic_by_topic_id(-100999, 350) is None  # same number, other group

    sent = await db.get_sent_message(group, 11, 350)
    assert sent is not None and sent.hde_entity_id == 900
    assert await db.get_sent_message(-100999, 11, 350) is None
    pending = await db.get_ai_feedback_pending(group, 350)
    assert pending is not None and pending["ticket_id"] == "T1"
    assert await db.get_ai_feedback_pending(-100999, 350) is None
    general = await db.list_general_messages_for("T2")
    assert [(g["chat_id"], g["message_id"]) for g in general] == [(group, 70)]

    async with aiosqlite.connect(set_test_db) as conn:
        async with conn.execute("SELECT chat_id, file_id FROM topic_media_cache") as cur:
            assert await cur.fetchall() == [(group, "F")]
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%_legacy'"
        ) as cur:
            assert await cur.fetchall() == []


@pytest.mark.asyncio
async def test_two_groups_keep_separate_rows_for_the_same_topic_number(initialized_db):
    await db.save_sent_message(-100111, 11, 350, "T1", 900, "post")
    await db.save_sent_message(-100222, 11, 350, "T2", 901, "post")
    assert (await db.get_sent_message(-100111, 11, 350)).ticket_id == "T1"
    assert (await db.get_sent_message(-100222, 11, 350)).ticket_id == "T2"
    await db.save_ai_feedback_pending(-100111, 350, "T1", "h", "t", "2999-01-01T00:00:00+00:00")
    await db.save_ai_feedback_pending(-100222, 350, "T2", "h", "t", "2999-01-01T00:00:00+00:00")
    assert (await db.get_ai_feedback_pending(-100222, 350))["ticket_id"] == "T2"
    await db.upsert_topic("T1", 350, chat_id=-100111)
    await db.upsert_topic("T2", 350, chat_id=-100222)
    assert (await db.get_topic_by_topic_id(-100222, 350)).ticket_id == "T2"
