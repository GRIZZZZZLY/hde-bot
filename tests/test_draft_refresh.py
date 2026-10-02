"""Пересборка черновика, когда коллега дописал комментарий.

Черновик рождается по первому сообщению клиента. Комментарий первой линии
(«звонил в банк», «у клиента Эвотор 7.3», «выезд назначен») появляется позже, и
до этой правки никак на черновик не влиял: агент стартует один раз, а отдельного
вебхука на внутренний комментарий в HelpDeskEddy нет — HANDLERS в
bot/hde_webhook.py знает только client_reply / staff_reply / ticket_updated.

Отсюда джоб: он замечает такие комментарии сам. Ограничения жёсткие, потому что
TPM у Groq free-tier 8000 и половина тикетов уже сегодня режет историю по
бюджету.
"""
from types import SimpleNamespace

import bot.config as config_module
from bot.agent.draft_refresh import refresh_stale_drafts


def _post(pid, uid, text, is_comment=False):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text, is_comment=is_comment,
                           date_created="00:00:00 01.01.2024", files=[])


def _sug(sid=1, ticket="T1", anchor="10", trigger="first"):
    return {"id": sid, "ticket_id": ticket, "topic_id": 100 + sid,
            "context_until_post_id": anchor, "trigger_source": trigger,
            "title": "Тема", "ai_answer": "старый черновик"}


async def _noop_sleep(_):
    return None


def _regen_spy(result="ok"):
    calls = []

    async def _regen(*, ticket_id, topic_id, reason):
        calls.append({"ticket_id": ticket_id, "topic_id": topic_id, "reason": reason})
        return result

    _regen.calls = calls
    return _regen


async def test_refresh_triggers_on_new_colleague_comment(monkeypatch):
    monkeypatch.setattr(config_module.config, "agent_draft_refresh_enabled", True,
                        raising=False)
    regen = _regen_spy()

    async def _stale(hours, limit):
        return [_sug()]

    async def _comments(ticket_id):
        return [_post(11, "colleague", "звонил в банк, QR генерирует эквайер",
                      is_comment=True)]

    async def _already(ticket_id):
        return False

    stats = await refresh_stale_drafts(
        _stale_fn=_stale, _comments_fn=_comments, _refreshed_fn=_already,
        _regen_fn=regen, _sleep_fn=_noop_sleep, _staff={"colleague"},
    )
    assert stats["refreshed"] == 1
    assert regen.calls[0]["ticket_id"] == "T1"
    assert "банк" in regen.calls[0]["reason"]


async def test_no_refresh_when_comment_predates_the_draft(monkeypatch):
    """Комментарий с post_id ниже якоря агент уже видел при генерации."""
    monkeypatch.setattr(config_module.config, "agent_draft_refresh_enabled", True,
                        raising=False)
    regen = _regen_spy()

    async def _stale(hours, limit):
        return [_sug(anchor="10")]

    async def _comments(ticket_id):
        return [_post(5, "colleague", "старый комментарий", is_comment=True)]

    async def _already(ticket_id):
        return False

    stats = await refresh_stale_drafts(
        _stale_fn=_stale, _comments_fn=_comments, _refreshed_fn=_already,
        _regen_fn=regen, _sleep_fn=_noop_sleep, _staff={"colleague"},
    )
    assert stats["refreshed"] == 0 and regen.calls == []


async def test_no_refresh_for_client_visible_posts(monkeypatch):
    """Публичный пост клиента — не повод: под ним уже висит кнопка
    «💡 Предложить ответ», и решение звать модель принимает оператор."""
    monkeypatch.setattr(config_module.config, "agent_draft_refresh_enabled", True,
                        raising=False)
    regen = _regen_spy()

    async def _stale(hours, limit):
        return [_sug()]

    async def _comments(ticket_id):
        return [_post(11, "cl", "ещё сообщение клиента", is_comment=False)]

    async def _already(ticket_id):
        return False

    stats = await refresh_stale_drafts(
        _stale_fn=_stale, _comments_fn=_comments, _refreshed_fn=_already,
        _regen_fn=regen, _sleep_fn=_noop_sleep, _staff={"colleague"},
    )
    assert stats["refreshed"] == 0


async def test_only_one_refresh_per_ticket(monkeypatch):
    """Второй раз по тому же тикету не ходим: у комментариев нет предела, а у
    TPM есть. Один пересчёт закрывает типовой случай «первая линия дописала»."""
    monkeypatch.setattr(config_module.config, "agent_draft_refresh_enabled", True,
                        raising=False)
    regen = _regen_spy()

    async def _stale(hours, limit):
        return [_sug()]

    async def _comments(ticket_id):
        return [_post(11, "colleague", "новый комментарий", is_comment=True)]

    async def _already(ticket_id):
        return True                       # пересборка по этому тикету уже была

    stats = await refresh_stale_drafts(
        _stale_fn=_stale, _comments_fn=_comments, _refreshed_fn=_already,
        _regen_fn=regen, _sleep_fn=_noop_sleep, _staff={"colleague"},
    )
    assert stats["skipped"] == 1 and regen.calls == []


async def test_disabled_flag_stops_everything(monkeypatch):
    monkeypatch.setattr(config_module.config, "agent_draft_refresh_enabled", False,
                        raising=False)
    called = {"v": False}

    async def _stale(hours, limit):
        called["v"] = True
        return []

    stats = await refresh_stale_drafts(_stale_fn=_stale, _sleep_fn=_noop_sleep)
    assert stats == {"refreshed": 0, "skipped": 0, "errors": 0}
    assert called["v"] is False


async def test_one_ticket_failure_does_not_stop_the_pass(monkeypatch):
    monkeypatch.setattr(config_module.config, "agent_draft_refresh_enabled", True,
                        raising=False)

    async def _stale(hours, limit):
        return [_sug(1, "BAD"), _sug(2, "GOOD")]

    async def _comments(ticket_id):
        if ticket_id == "BAD":
            raise RuntimeError("HDE 503")
        return [_post(11, "colleague", "комментарий", is_comment=True)]

    async def _already(ticket_id):
        return False

    regen = _regen_spy()
    stats = await refresh_stale_drafts(
        _stale_fn=_stale, _comments_fn=_comments, _refreshed_fn=_already,
        _regen_fn=regen, _sleep_fn=_noop_sleep, _staff={"colleague"},
    )
    assert stats == {"refreshed": 1, "skipped": 0, "errors": 1}
    assert [c["ticket_id"] for c in regen.calls] == ["GOOD"]


async def test_regeneration_failure_counts_as_error(monkeypatch):
    monkeypatch.setattr(config_module.config, "agent_draft_refresh_enabled", True,
                        raising=False)

    async def _stale(hours, limit):
        return [_sug()]

    async def _comments(ticket_id):
        return [_post(11, "colleague", "комментарий", is_comment=True)]

    async def _already(ticket_id):
        return False

    async def _regen(*, ticket_id, topic_id, reason):
        return None                       # модель не ответила

    stats = await refresh_stale_drafts(
        _stale_fn=_stale, _comments_fn=_comments, _refreshed_fn=_already,
        _regen_fn=_regen, _sleep_fn=_noop_sleep, _staff={"colleague"},
    )
    assert stats["errors"] == 1 and stats["refreshed"] == 0


async def test_limit_caps_llm_calls_per_pass(monkeypatch):
    """Лимит передаётся в выборку, а не фильтруется после: иначе один
    разговорчивый день выбирает всю квоту токенов на сутки вперёд."""
    monkeypatch.setattr(config_module.config, "agent_draft_refresh_enabled", True,
                        raising=False)
    seen = {}

    async def _stale(hours, limit):
        seen["limit"], seen["hours"] = limit, hours
        return []

    await refresh_stale_drafts(_stale_fn=_stale, _sleep_fn=_noop_sleep, limit=3, hours=6)
    assert seen == {"limit": 3, "hours": 6}


# --- выборка кандидатов в базе ---------------------------------------------


async def test_list_stale_drafts_skips_sent_and_old():
    import bot.db as db
    from bot.db.core import connect
    await db.init_db()

    async def _add(ticket, *, trigger="first", delivery="not_sent", days_ago=0):
        sid = await db.record_suggestion(
            ticket_id=ticket, topic_id=1, chat_id=config_module.config.group_chat_id, trigger_source=trigger,
            context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
            ai_answer="черновик",
        )
        async with connect() as conn:
            await conn.execute(
                "UPDATE ai_suggestions SET delivery_status=?, "
                "created_at=datetime('now', ?) WHERE id=?",
                (delivery, f"-{days_ago} days", sid),
            )
            await conn.commit()
        return sid

    await _add("FRESH")
    await _add("SENT", delivery="sent")
    await _add("OLD", days_ago=5)
    await _add("BUTTON", trigger="button")

    rows = await db.list_stale_drafts(hours=24, limit=10)
    tickets = {r["ticket_id"] for r in rows}
    assert "FRESH" in tickets
    assert "SENT" not in tickets      # оператор уже ответил клиенту
    assert "OLD" not in tickets       # тикет за окном, пересчёт бесполезен
    assert "BUTTON" not in tickets    # черновик по кнопке оператор уже видел свежим


async def test_ticket_already_refreshed_detects_comment_trigger():
    import bot.db as db
    await db.init_db()
    await db.record_suggestion(
        ticket_id="REF1", topic_id=1, chat_id=config_module.config.group_chat_id, trigger_source="comment",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
        ai_answer="обновлённый черновик",
    )
    assert await db.ticket_draft_refreshed("REF1") is True
    assert await db.ticket_draft_refreshed("REF-НЕТ") is False
