"""Тихий режим: флаги гасят шумные сообщения и не трогают остальное."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import zoneinfo

import pytest

from bot import scheduler, topic_manager
from bot.config import config


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_history_dump_off_but_autofill_still_runs(monkeypatch):
    """Дамп переписки не уходит в топик, автозаполнение полей остаётся."""
    from bot.hde_api import HDEPost, HDETicketInfo

    monkeypatch.setattr(config, "ticket_history_post_enabled", False)
    monkeypatch.setattr(config, "has_hde_api_credentials", lambda: True)
    apply_mock = AsyncMock()
    format_mock = MagicMock(return_value=["история"])

    with (
        patch("bot.hde_api.HDEApiClient") as MockClient,
        patch("bot.topic_manager._post_client_history", new_callable=AsyncMock),
        patch("bot.topic_manager._generate_summary_with_retry", new_callable=AsyncMock) as gen,
        patch("bot.topic_manager.format_ticket_history", format_mock),
        patch("bot.topic_manager.db") as mock_db,
        patch("bot.ticket_fields.apply_ticket_fields", apply_mock),
    ):
        instance = MockClient.return_value
        instance.get_ticket_info = AsyncMock(
            return_value=HDETicketInfo(client_id=1, client_name="A", owner_id=2, owner_name="B")
        )
        instance.get_ticket_posts = AsyncMock(return_value=[
            HDEPost(post_id=1, user_id=1, text="Привет",
                    date_created="10:00:00 01.01.2026", is_comment=False)
        ])
        instance.get_ticket_comments = AsyncMock(return_value=[])
        gen.return_value = None
        mock_db.update_topic = AsyncMock()

        await topic_manager._post_ticket_history(_bot(), "TKT-1", config.group_chat_id, 555, ticket_title="T")

    format_mock.assert_not_called()
    apply_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_suggestion_off_skips_generation(monkeypatch):
    monkeypatch.setattr(config, "ai_suggestion_auto_enabled", False)
    monkeypatch.setattr(config, "agent_enabled", True)
    with patch("bot.topic_manager.run_agent", new_callable=AsyncMock) as agent, patch(
        "bot.topic_manager.generate_ticket_summary", new_callable=AsyncMock
    ) as legacy:
        result = await topic_manager._generate_summary_with_retry(
            [SimpleNamespace(user_id=1, text="q", post_id=1)],
            SimpleNamespace(client_id=1), ticket_id="T1",
        )
    assert result is None
    agent.assert_not_awaited()
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_suggest_button_still_generates_when_auto_off(monkeypatch):
    """Кнопка 💡 приходит с trigger_source='button' и флагом не гасится."""
    monkeypatch.setattr(config, "ai_suggestion_auto_enabled", False)
    monkeypatch.setattr(config, "agent_enabled", False)
    with patch(
        "bot.topic_manager.generate_ticket_summary",
        new=AsyncMock(return_value=("с", "к", "п", 70)),
    ) as legacy:
        result = await topic_manager._generate_summary_with_retry(
            [SimpleNamespace(user_id=1, text="q", post_id=1)],
            SimpleNamespace(client_id=1), ticket_id="T2", trigger_source="button",
            chat_id=config.group_chat_id,
        )
    assert result == ("с", "к", "п", 70)
    legacy.assert_awaited()


@pytest.mark.asyncio
async def test_morning_digest_off(monkeypatch):
    monkeypatch.setattr(config, "morning_digest_enabled", False)
    monkeypatch.setattr(config, "digest_send_hour_utc", datetime.now(timezone.utc).hour)
    with patch("bot.digest.send_morning_digest", new_callable=AsyncMock) as send:
        await scheduler._maybe_send_digest(_bot())
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_value_report_off(monkeypatch):
    monkeypatch.setattr(config, "personal_digests_enabled", False)
    monkeypatch.setattr(config, "digest_send_hour_utc", datetime.now(timezone.utc).hour)
    with patch("bot.agent.value_report.send_daily_value_report", new_callable=AsyncMock) as send:
        await scheduler._maybe_send_daily_value_report(_bot())
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_nightly_reconcile_off(monkeypatch):
    monkeypatch.setattr(config, "nightly_reconcile_enabled", False)
    monkeypatch.setattr(config, "agent_dialogue_mining_enabled", True)
    at_two = datetime(2026, 9, 15, 2, 30, tzinfo=zoneinfo.ZoneInfo("Europe/Moscow"))
    monkeypatch.setattr(scheduler, "_now_msk", lambda: at_two)
    monkeypatch.setattr(scheduler, "_last_reconcile_date", None)
    with patch("bot.agent.reconcile.reconcile_recent", new_callable=AsyncMock) as rec:
        await scheduler._maybe_reconcile_answers(_bot())
    rec.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_never_falls_back_to_legacy_summary(monkeypatch):
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    monkeypatch.setattr(config, "agent_enabled", True)
    monkeypatch.setattr(config, "agent_auto_first_suggestion_enabled", True)
    monkeypatch.setattr(config, "ai_suggestion_auto_enabled", True)
    with patch("bot.topic_manager.run_agent", new_callable=AsyncMock) as agent, patch(
        "bot.topic_manager.generate_ticket_summary", new_callable=AsyncMock
    ) as legacy:
        agent.return_value = None
        result = await topic_manager._generate_summary_with_retry(
            [], SimpleNamespace(client_id=1), ticket_id="T", trigger_source="reply",
        )
    assert result is None
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_measures_but_does_not_distill_by_default(monkeypatch):
    monkeypatch.setattr(config, "nightly_reconcile_enabled", True)
    monkeypatch.setattr(config, "agent_dialogue_mining_enabled", True)
    monkeypatch.setattr(config, "reconcile_kb_distill_enabled", False)
    monkeypatch.setattr(scheduler, "_last_reconcile_date", None)
    monkeypatch.setattr(scheduler, "_now_msk", lambda: datetime(2026, 9, 28, 2, 5,
                        tzinfo=zoneinfo.ZoneInfo("Europe/Moscow")))
    with patch("bot.agent.reconcile.reconcile_recent", new=AsyncMock(return_value={})) as rec, \
         patch("bot.agent.kb_distill.process_pending_candidates", new=AsyncMock()) as distill, \
         patch.object(scheduler.db, "archive_unused_auto_rules", new=AsyncMock()) as archive:
        await scheduler._maybe_reconcile_answers(MagicMock())
    rec.assert_awaited_once()
    distill.assert_not_awaited()
    archive.assert_not_awaited()
