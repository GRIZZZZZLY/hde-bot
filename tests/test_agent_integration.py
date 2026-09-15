# tests/test_agent_integration.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot.config as config_module
import bot.db as db_module
from bot.db.suggestion_store import compute_idempotency_key, get_open_suggestion_by_topic
from bot.handlers.ai_feedback import register_feedback_pending


async def test_register_always_records_with_anchor_and_tag(monkeypatch):
    await db_module.init_db()
    monkeypatch.setattr(config_module.config, "agent_enabled", True)  # флаг не влияет
    await register_feedback_pending(
        topic_id=1, ticket_id="T1", history="h", title="t",
        answer_text="a", ai_full_text="f", context_until_post_id="42",
    )
    row = await get_open_suggestion_by_topic(1)
    assert row is not None
    assert row["context_until_post_id"] == "42"
    assert row["prompt_version"] in ("legacy", "db-active")


async def test_agent_row_and_register_row_dedupe_to_one(monkeypatch):
    """Агент записал строку; register с теми же ключами не создаёт дубль."""
    await db_module.init_db()
    from bot.ai_summary import prompt_version_tag
    from bot.db.suggestion_store import record_suggestion

    kw = dict(ticket_id="T2", trigger_source="first", context_until_post_id="10",
              pipeline_version=config_module.config.agent_pipeline_version,
              prompt_version=prompt_version_tag())
    sid_agent = await record_suggestion(topic_id=5, action_type="ANSWER", **kw)
    await register_feedback_pending(
        topic_id=5, ticket_id="T2", history="h", title="t",
        answer_text="a", ai_full_text="f", context_until_post_id="10",
    )
    row = await get_open_suggestion_by_topic(5)
    assert row["id"] == sid_agent                     # та же строка
    assert row["action_type"] == "ANSWER"             # trace агента не затёрт


async def test_generate_with_retry_agent_branch_and_fallback(monkeypatch):
    import bot.topic_manager as tm
    monkeypatch.setattr(config_module.config, "agent_enabled", True)
    monkeypatch.setattr(config_module.config, "agent_auto_first_suggestion_enabled", True)
    monkeypatch.setattr(config_module.config, "ai_suggestion_auto_enabled", True)
    posts = [SimpleNamespace(user_id=1, text="q", post_id=1)]
    info = SimpleNamespace(client_id=1)

    with patch("bot.topic_manager.run_agent", new=AsyncMock(
        return_value=("суть", "клиенту", "памятка", 88)
    )) as ok_agent, patch(
        "bot.topic_manager.generate_ticket_summary", new=AsyncMock()
    ) as old:
        result = await tm._generate_summary_with_retry(
            posts, info, ticket_title="t", ticket_id="T3", topic_id=7,
        )
    assert result == ("суть", "клиенту", "памятка", 88)
    ok_agent.assert_awaited()
    assert ok_agent.await_args.kwargs["topic_id"] == 7
    old.assert_not_awaited()

    # агент упал → легаси путь отработал
    with patch("bot.topic_manager.run_agent", new=AsyncMock(
        side_effect=RuntimeError("boom")
    )), patch("bot.topic_manager.generate_ticket_summary", new=AsyncMock(
        return_value=("с", "к", "п", 50)
    )) as old2:
        result = await tm._generate_summary_with_retry(
            posts, info, ticket_title="t", ticket_id="T4", topic_id=7,
        )
    assert result == ("с", "к", "п", 50)
    old2.assert_awaited()


async def test_agent_enabled_but_auto_first_disabled_uses_legacy(monkeypatch):
    import bot.topic_manager as tm
    monkeypatch.setattr(config_module.config, "agent_enabled", True)
    monkeypatch.setattr(config_module.config, "agent_auto_first_suggestion_enabled", False)
    monkeypatch.setattr(config_module.config, "ai_suggestion_auto_enabled", True)
    with patch("bot.topic_manager.run_agent", new=AsyncMock()) as agent, patch(
        "bot.topic_manager.generate_ticket_summary",
        new=AsyncMock(return_value=("с", "к", "п", 50)),
    ):
        result = await tm._generate_summary_with_retry(
            [SimpleNamespace(user_id=1, text="q", post_id=1)],
            SimpleNamespace(client_id=1), ticket_title="t", ticket_id="T5",
        )
    assert result == ("с", "к", "п", 50)
    agent.assert_not_awaited()
