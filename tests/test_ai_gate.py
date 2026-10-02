"""Model calls only for engineers with ai_enabled: a colleague's ticket must not touch the shared quota."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.db as db_module
from bot import ai_summary, hde_api, operators, ticket_fields, topic_history, topic_manager, transcription, vision
from bot.config import config
from bot.hde_api import HDEAttachment, HDEPost, HDETicketInfo

COLLEAGUE_CHAT = -100222


def _payload(**overrides):
    payload = {
        "ticket_id": "TKT-C", "unique_id": "C-1", "ticket_name": "Не печатает чек",
        "company_name": "ACME", "priority": "high", "status": "open",
        "owner_id": "102", "owner_name": "Максим", "user_name": "Alice",
        "message": "Помогите", "last_post_date": "2026-10-02 12:00:00",
        "sla_remaining_minutes": "30", "link": "https://hde.example.com/tickets/1",
    }
    payload.update(overrides)
    return payload


def _bot():
    bot = AsyncMock()
    forum_topic = MagicMock()
    forum_topic.message_thread_id = 777
    bot.create_forum_topic = AsyncMock(return_value=forum_topic)
    bot.send_message.return_value.message_id = 1001
    return bot


def _hde_client():
    client = MagicMock()
    client.get_ticket_info = AsyncMock(
        return_value=HDETicketInfo(client_id=1, client_name="A", owner_id=2, owner_name="B")
    )
    client.get_ticket_posts = AsyncMock(return_value=[
        HDEPost(post_id=1, user_id=1, text="Не печатает", date_created="10:00:00 02.10.2026"),
    ])
    client.get_ticket_comments = AsyncMock(return_value=[
        HDEPost(post_id=2, user_id=102, text="Проверь ФН", date_created="10:05:00 02.10.2026",
                is_comment=True),
    ])
    return client


@pytest.fixture
def models(monkeypatch, initialized_db):
    """A colleague with AI off, every AI feature on, and the lowest-level model calls mocked."""
    monkeypatch.setattr(operators, "COLLEAGUES", (
        operators.Operator("102", "Максим", 1220214456, COLLEAGUE_CHAT),
    ))
    for flag in ("ai_suggestion_auto_enabled", "agent_enabled", "agent_voice_v2_enabled",
                 "agent_reply_drafts_enabled", "agent_draft_refresh_enabled"):
        monkeypatch.setattr(config, flag, True)
    monkeypatch.setattr(config, "has_hde_api_credentials", lambda: True)
    monkeypatch.setattr(topic_manager, "_is_work_time", lambda: True)
    monkeypatch.setattr(topic_manager, "_post_client_history", AsyncMock())
    client = _hde_client()
    monkeypatch.setattr(hde_api, "HDEApiClient", lambda *a, **k: client)
    monkeypatch.setattr(ticket_fields, "HDEApiClient", lambda *a, **k: client)
    mocks = {
        "run_agent": AsyncMock(return_value=("суть", "клиенту", "памятка", 80)),
        "generate_ticket_summary": AsyncMock(return_value=("суть", "клиенту", "памятка", 80)),
        "describe_image": AsyncMock(return_value="экран с ошибкой"),
        "classify_environment": AsyncMock(return_value=None),
        "classify_priority_type": AsyncMock(return_value=None),
        "_transcribe_audio_posts": AsyncMock(return_value=[]),
        "transcribe_audio": AsyncMock(return_value="текст"),
    }
    monkeypatch.setattr(topic_manager, "run_agent", mocks["run_agent"])
    monkeypatch.setattr(topic_manager, "generate_ticket_summary", mocks["generate_ticket_summary"])
    monkeypatch.setattr(vision, "describe_image", mocks["describe_image"])
    monkeypatch.setattr(ticket_fields, "classify_environment", mocks["classify_environment"])
    monkeypatch.setattr(ticket_fields, "classify_priority_type", mocks["classify_priority_type"])
    monkeypatch.setattr(ai_summary, "_transcribe_audio_posts", mocks["_transcribe_audio_posts"])
    monkeypatch.setattr(transcription, "transcribe_audio", mocks["transcribe_audio"])
    return mocks


def _assert_no_model_calls(mocks):
    for name, mock in mocks.items():
        assert not mock.called, f"{name} was called for a colleague with AI off"


async def _colleague_topic(**fields):
    await db_module.upsert_topic(
        "TKT-C", 777, chat_id=COLLEAGUE_CHAT, ticket_name="Не печатает чек",
        owner_id="102", owner_name="Максим", **fields,
    )


async def test_new_colleague_ticket_gets_no_model_calls(models):
    bot = _bot()

    await topic_manager.handle_assigned_on_create(bot, _payload())

    assert bot.create_forum_topic.call_args.kwargs["chat_id"] == COLLEAGUE_CHAT
    _assert_no_model_calls(models)


async def test_new_primary_ticket_still_calls_the_model(models, monkeypatch):
    monkeypatch.setattr(topic_history, "post_suggestion_messages", AsyncMock(return_value=True))
    apply_fields = AsyncMock()
    monkeypatch.setattr(ticket_fields, "apply_ticket_fields", apply_fields)

    await topic_manager.handle_assigned_on_create(_bot(), _payload(owner_id="me", owner_name="Me"))

    models["run_agent"].assert_awaited_once()
    assert models["run_agent"].await_args.kwargs["chat_id"] == config.group_chat_id
    apply_fields.assert_awaited_once()


async def test_colleague_client_reply_with_photo_gets_no_model_calls(models, monkeypatch):
    await _colleague_topic()
    await db_module.update_topic("TKT-C", env_option_id="")  # would trigger env re-classification
    monkeypatch.setattr(topic_manager, "download_client_attachment", AsyncMock(
        return_value=HDEAttachment(filename="err.jpg", content=b"img", content_type="image/jpeg")
    ))
    reply_draft = AsyncMock(return_value=True)
    env_retry = AsyncMock()
    monkeypatch.setattr(topic_history, "append_draft_to_reply", reply_draft)
    monkeypatch.setattr(ticket_fields, "retry_env_classification", env_retry)
    bot = _bot()

    await topic_manager.handle_client_reply(
        bot, _payload(attachments=[{"url": "https://hde.example.com/f/err.jpg", "filename": "err.jpg"}])
    )

    bot.send_photo.assert_awaited_once()  # the photo itself is still delivered
    reply_draft.assert_not_called()
    env_retry.assert_not_called()
    _assert_no_model_calls(models)


async def test_suggest_button_in_colleague_group_answers_ai_off(models):
    from bot.handlers.ai_feedback import cb_ai_suggest

    await _colleague_topic()
    callback = MagicMock()
    callback.message.message_thread_id = 777
    callback.message.chat.id = COLLEAGUE_CHAT
    callback.answer = AsyncMock()
    callback.bot = _bot()

    await cb_ai_suggest(callback)

    callback.answer.assert_awaited_once_with(operators.AI_OFF_NOTE, show_alert=False)
    callback.bot.send_message.assert_not_called()
    _assert_no_model_calls(models)


async def test_missing_summary_retry_skips_colleague_but_not_primary(models, monkeypatch):
    monkeypatch.setattr(topic_history, "post_suggestion_messages", AsyncMock(return_value=True))
    await _colleague_topic(last_client_reply_at="2026-10-02 09:00:00")
    await db_module.upsert_topic(
        "TKT-P", 778, chat_id=config.group_chat_id, owner_id="me",
        last_client_reply_at="2026-10-02 09:30:00",
    )

    sent = await topic_manager.retry_missing_ai_summaries(_bot())

    assert sent == 1
    models["run_agent"].assert_awaited_once()
    assert models["run_agent"].await_args.kwargs["ticket_id"] == "TKT-P"


async def test_scheduled_draft_refresh_skips_colleague(models, monkeypatch):
    from bot.agent.draft_refresh import refresh_stale_drafts
    from bot.handlers import ai_feedback

    # regenerate_draft imports this name from ai_feedback, where it does not live;
    # provide it so the pass reaches the model gate instead of dying on ImportError.
    monkeypatch.setattr(ai_feedback, "post_suggestion_messages", AsyncMock(), raising=False)
    await _colleague_topic()

    stats = await refresh_stale_drafts(
        _bot(),
        _stale_fn=AsyncMock(return_value=[
            {"ticket_id": "TKT-C", "topic_id": 777, "context_until_post_id": "1"},
        ]),
        _refreshed_fn=AsyncMock(return_value=False),
        _comments_fn=AsyncMock(return_value=_hde_client().get_ticket_comments.return_value),
        _staff={"102"},
        _sleep_fn=AsyncMock(),
    )

    assert stats["refreshed"] == 0
    _assert_no_model_calls(models)


async def test_autofill_skips_classification_for_colleague(models):
    result = await ticket_fields.apply_ticket_fields(_bot(), "TKT-C", COLLEAGUE_CHAT, 777, "Эвотор завис")

    assert result.updated is False
    assert result.error == operators.AI_OFF_NOTE
    _assert_no_model_calls(models)


def test_ai_enabled_for():
    assert operators.ai_enabled_for(chat_id=config.group_chat_id) is True
    assert operators.ai_enabled_for(owner_id=config.hde_owner_id) is True
    assert operators.ai_enabled_for(chat_id=-1) is False
    assert operators.ai_enabled_for() is False
