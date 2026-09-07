"""Очередь кандидатов в базу знаний из вердиктов сверки (п.4).

Вердикт bot_wrong_fact означает: оператор ответил иначе и по существу, то есть
в базе знаний либо нет статьи, либо она врёт. Такой случай попадает в очередь, а
не в базу напрямую: категорию ставит LLM-судья, и его ошибка иначе уехала бы в
RAG без единого человеческого взгляда.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.db as db_module
from bot.db.kb_candidates import (
    list_pending_kb_candidates,
    save_kb_candidate,
    set_kb_candidate_status,
)


def _post(pid, uid, text, is_comment=False, dc="00:00:00 01.01.2024"):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text,
                           is_comment=is_comment, date_created=dc)


async def _candidate(suggestion_id=1, ticket_id="T1"):
    return await save_kb_candidate(
        suggestion_id=suggestion_id,
        ticket_id=ticket_id,
        title="Терминал Сбербанка не проводит оплату",
        history="Клиент: терминал не проводит оплату",
        ai_answer="Настройку терминала выполняет только банк.",
        reference_answer="Подключился и настроил терминал сам, дело было в IP-адресе",
        reason="настройка выполнима удалённо, банк не нужен",
    )


async def test_candidate_is_saved_once_per_suggestion():
    await db_module.init_db()
    first = await _candidate(suggestion_id=101)
    again = await _candidate(suggestion_id=101)
    assert first is not None
    assert again is None                      # повторная сверка не дублирует очередь


async def test_list_pending_returns_only_undecided():
    await db_module.init_db()
    keep = await _candidate(suggestion_id=201, ticket_id="K1")
    decided = await _candidate(suggestion_id=202, ticket_id="K2")
    await set_kb_candidate_status(decided, "skipped")
    pending = await list_pending_kb_candidates()
    ids = [c["id"] for c in pending]
    assert keep in ids and decided not in ids


async def test_decision_is_idempotent():
    await db_module.init_db()
    cid = await _candidate(suggestion_id=301)
    row = await set_kb_candidate_status(cid, "added")
    assert row is not None and row["ticket_id"] == "T1"
    assert await set_kb_candidate_status(cid, "added") is None   # второй клик — no-op


async def test_apply_candidate_indexes_operator_answer_and_marks_added():
    from bot.agent.kb_candidates import apply_kb_candidate
    await db_module.init_db()
    cid = await _candidate(suggestion_id=401, ticket_id="188203")
    index = AsyncMock()
    row = await apply_kb_candidate(cid, add=True, _index_fn=index)
    assert row is not None
    index.assert_awaited_once()
    kwargs = index.await_args.kwargs
    assert kwargs["source"] == "reconcile"
    assert kwargs["ticket_id"] == "188203"
    assert kwargs["quality"] == "corrected"
    assert "Подключился и настроил терминал сам" in kwargs["content"]
    assert "терминал не проводит оплату" in kwargs["content"]
    assert await list_pending_kb_candidates() == []


async def test_apply_candidate_skip_does_not_index():
    from bot.agent.kb_candidates import apply_kb_candidate
    await db_module.init_db()
    cid = await _candidate(suggestion_id=501)
    index = AsyncMock()
    row = await apply_kb_candidate(cid, add=False, _index_fn=index)
    assert row is not None
    index.assert_not_awaited()
    assert await list_pending_kb_candidates() == []


async def test_reconcile_queues_candidate_only_for_wrong_fact():
    from bot.agent.reconcile import reconcile_recent
    suggestions = [
        {"id": 11, "ticket_id": "W1", "ai_answer": "настройку делает только банк",
         "context_until_post_id": "1", "title": "терминал", "history": "Клиент: не платит"},
        {"id": 12, "ticket_id": "E1", "ai_answer": "передадим специалисту",
         "context_until_post_id": "1", "title": "чек", "history": "Клиент: не печатает"},
    ]
    posts_by = {
        "W1": [_post(1, "cl", "q"), _post(2, "op", "Настроил терминал сам, дело в IP")],
        "E1": [_post(1, "cl", "q"), _post(2, "op", "Обновил драйвер, пробуйте печатать")],
    }
    verdict_by_ticket = {
        "Настроил терминал сам, дело в IP": ("bot_wrong_fact", "банк не нужен"),
        "Обновил драйвер, пробуйте печатать": ("bot_escalated", "решил сам"),
    }

    async def sug_fn(hours):
        return suggestions

    async def posts_fn(tid):
        return posts_by[tid]

    async def judge_fn(ai_answer, reference, **kwargs):
        return verdict_by_ticket[reference]

    async def set_fn(sid, **kwargs):
        return None

    async def sleep_fn(_seconds):
        return None

    queued = []

    async def candidate_fn(**kwargs):
        queued.append(kwargs)
        return 1

    stats = await reconcile_recent(
        hours=24, _suggestions_fn=sug_fn, _posts_fn=posts_fn, _set_fn=set_fn,
        _staff={"op"}, _judge_fn=judge_fn, _sleep_fn=sleep_fn,
        _candidate_fn=candidate_fn,
    )
    assert stats["bot_wrong_fact"] == 1 and stats["bot_escalated"] == 1
    assert len(queued) == 1
    assert queued[0]["suggestion_id"] == 11
    assert queued[0]["ticket_id"] == "W1"
    assert queued[0]["reference_answer"] == "Настроил терминал сам, дело в IP"
    assert queued[0]["reason"] == "банк не нужен"


async def test_digest_sends_one_message_per_conflict():
    """С 2026-09-07 сообщение с кнопками получает только противоречие: очередь
    pending разбирается автоматически (bot/agent/kb_distill.py), и десять
    решений по фактам каждое утро больше не приходят."""
    import json
    from bot.digest import send_kb_conflicts
    bot = SimpleNamespace(send_message=AsyncMock())
    candidates = [
        {"id": 7, "ticket_id": "188203", "title": "терминал",
         "ai_answer": "только банк", "reference_answer": "настроил сам",
         "reason": "адресат другой",
         "rule_json": json.dumps({"symptom": "s", "rule": "настройку делает банк",
                                  "action": "a"}, ensure_ascii=False),
         "conflict_item_id": 5},
    ]

    async def list_fn(limit=10):
        return candidates

    await send_kb_conflicts(bot, _list_fn=list_fn)
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert "188203" in kwargs["text"]
    assert "настройку делает банк" in kwargs["text"]
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["kbc:new:7", "kbc:old:7"]


async def test_digest_silent_without_conflicts():
    from bot.digest import send_kb_conflicts
    bot = SimpleNamespace(send_message=AsyncMock())

    async def list_fn(limit=10):
        return []

    await send_kb_conflicts(bot, _list_fn=list_fn)
    bot.send_message.assert_not_awaited()


async def test_conflict_message_survives_broken_rule_json():
    """Битый rule_json не должен глотать сообщение: противоречие всё равно
    нужно показать, пусть и без текста нового правила."""
    from bot.digest import send_kb_conflicts
    bot = SimpleNamespace(send_message=AsyncMock())

    async def list_fn(limit=10):
        return [{"id": 8, "ticket_id": "190000", "reference_answer": "ответ",
                 "reason": "спор", "rule_json": "{не json", "conflict_item_id": 5}]

    await send_kb_conflicts(bot, _list_fn=list_fn)
    bot.send_message.assert_awaited_once()
    assert "190000" in bot.send_message.await_args.kwargs["text"]
