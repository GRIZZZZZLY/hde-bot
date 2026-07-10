# tests/test_agent_pipeline.py
from types import SimpleNamespace

from bot.agent.pipeline import run_agent
from bot.agent.safety import PolicyDecision

_POSTS = [SimpleNamespace(user_id=1, text="касса не печатает", post_id=5)]
_INFO = SimpleNamespace(client_id=1)
_PROCEED = lambda t: PolicyDecision("PROCEED", None, None)


def _ctx_dict():
    return {"history": "Клиент: касса не печатает", "client_text": "касса не печатает",
            "equipment": "АТОЛ",
            "evidence": [{"source_type": "knowledge_item", "source_id": 12, "rank": 1,
                          "score": 0.9, "title": None, "used_excerpt": "перезагрузка помогает"}],
            "retrieval_query": "q", "wiki": None, "solution_steps": None,
            "grounds": ["KB#12"], "confidence": 90}


async def _ctx(*a, **k):
    return _ctx_dict()


async def _fresh_posts_same(ticket_id):
    return _POSTS


async def test_answer_path_records_full_trace():
    recorded = {}

    async def draft(ctx, title, **k):
        return {"action": "ANSWER", "suit": "чек", "client": "проверьте бумагу",
                "memo": "м", "confidence": 85, "confidence_reason": "kb совпал"}

    async def sc(ct, gen, evidence, hist, **k):
        assert evidence[0]["source_id"] == 12          # self-check получает evidence
        return {"status": "supported", "fallback_action": "ASK", "fallback_client_text": ""}

    async def rec(**kw):
        recorded.update(kw)
        return 1

    suit, client, memo, conf = await run_agent(
        _POSTS, _INFO, ticket_title="Не печатает", ticket_id="T1", topic_id=77,
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=sc,
        _safety_pre=_PROCEED, _safety_post=_PROCEED,
        _record_fn=rec, _posts_fn=_fresh_posts_same,
    )
    assert client == "проверьте бумагу" and conf == 85
    assert recorded["topic_id"] == 77                  # реальный topic_id
    assert recorded["context_until_post_id"] == "5"
    assert recorded["action_type"] == "ANSWER"
    assert '"source_id": 12' in recorded["retrieved_refs"]
    assert recorded["retrieval_query"] == "q"
    assert recorded["generation_ms"] is not None


async def test_pre_policy_escalates_before_retrieval():
    ctx_called = {"v": False}

    async def spy_ctx(*a, **k):
        ctx_called["v"] = True
        return _ctx_dict()

    async def rec(**kw):
        return 1

    suit, client, memo, conf = await run_agent(
        _POSTS, _INFO, ticket_title="Возврат денег", ticket_id="T2",
        _context_fn=spy_ctx, _draft_fn=None, _selfcheck_fn=None,
        _safety_pre=lambda t: PolicyDecision("ESCALATE", "finance", "x"),
        _safety_post=_PROCEED, _record_fn=rec, _posts_fn=_fresh_posts_same,
    )
    assert ctx_called["v"] is False                    # retrieval не запускался
    assert client == "" and "ESCALATE" in memo


async def test_partially_supported_also_falls_back_with_conf_30():
    async def draft(ctx, title, **k):
        return {"action": "ANSWER", "suit": "s", "client": "полувыдумка",
                "memo": "m", "confidence": 85, "confidence_reason": ""}

    async def sc(ct, gen, evidence, hist, **k):
        return {"status": "partially_supported", "fallback_action": "ASK",
                "fallback_client_text": "уточните модель"}

    async def rec(**kw):
        return 1

    _, client, memo, conf = await run_agent(
        _POSTS, _INFO, ticket_title="t", ticket_id="T3",
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=sc,
        _safety_pre=_PROCEED, _safety_post=_PROCEED,
        _record_fn=rec, _posts_fn=_fresh_posts_same,
    )
    assert client == "уточните модель"
    assert conf == 30                                  # не 85 после fallback


async def test_superseded_triggers_one_regeneration():
    calls = {"draft": 0}
    fresh_versions = [
        [SimpleNamespace(user_id=1, text="касса не печатает", post_id=5),
         SimpleNamespace(user_id=1, text="уже перезагрузил", post_id=6)],
    ]

    async def moving_posts(ticket_id):
        return fresh_versions[0]

    async def draft(ctx, title, **k):
        calls["draft"] += 1
        return {"action": "ANSWER", "suit": "s", "client": f"ответ{calls['draft']}",
                "memo": "m", "confidence": 80, "confidence_reason": ""}

    async def sc(ct, gen, evidence, hist, **k):
        return {"status": "supported", "fallback_action": "ASK", "fallback_client_text": ""}

    async def rec(**kw):
        return 1

    _, client, memo, _ = await run_agent(
        _POSTS, _INFO, ticket_title="t", ticket_id="T4",
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=sc,
        _safety_pre=_PROCEED, _safety_post=_PROCEED,
        _record_fn=rec, _posts_fn=moving_posts,
    )
    assert calls["draft"] == 2                         # одна перегенерация
    # после второй попытки якорь=6 совпадает → без stale-предупреждения
    assert "новое сообщение" not in memo


async def test_post_safety_escalates_generated_answer():
    async def draft(ctx, title, **k):
        return {"action": "ANSWER", "suit": "s", "client": "Я сделаю возврат средств",
                "memo": "m", "confidence": 90, "confidence_reason": ""}

    async def rec(**kw):
        return 1

    _, client, memo, conf = await run_agent(
        _POSTS, _INFO, ticket_title="t", ticket_id="T8",
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=None,
        _safety_pre=_PROCEED,
        _safety_post=lambda t: PolicyDecision("ESCALATE", "finance", "возврат"),
        _record_fn=rec, _posts_fn=_fresh_posts_same,
    )
    assert client == "" and conf == 0
    assert "небезопасен" in memo


async def test_no_action_and_ask_paths():
    async def draft_no(ctx, title, **k):
        return {"action": "NO_ACTION", "suit": "спасибо", "client": "",
                "memo": "ответ не нужен", "confidence": 95, "confidence_reason": ""}

    async def rec(**kw):
        return 1

    _, client, memo, _ = await run_agent(
        _POSTS, _INFO, ticket_title="t", ticket_id="T5",
        _context_fn=_ctx, _draft_fn=draft_no, _selfcheck_fn=None,
        _safety_pre=_PROCEED, _safety_post=_PROCEED,
        _record_fn=rec, _posts_fn=_fresh_posts_same,
    )
    assert client == "" and "NO_ACTION" in memo


async def test_returns_none_on_draft_failure_and_record_failure_nonfatal():
    async def draft(ctx, title, **k):
        return None

    async def boom_rec(**kw):
        raise RuntimeError("db down")

    assert await run_agent(
        _POSTS, _INFO, ticket_title="t", ticket_id="T6",
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=None,
        _safety_pre=_PROCEED, _safety_post=_PROCEED,
        _record_fn=boom_rec, _posts_fn=_fresh_posts_same,
    ) is None                                          # драфт упал → legacy

    async def draft_ok(ctx, title, **k):
        return {"action": "ANSWER", "suit": "s", "client": "ок", "memo": "m",
                "confidence": 70, "confidence_reason": ""}

    async def sc(ct, gen, evidence, hist, **k):
        return {"status": "supported", "fallback_action": "ASK", "fallback_client_text": ""}

    result = await run_agent(                          # запись упала → результат живёт
        _POSTS, _INFO, ticket_title="t", ticket_id="T7",
        _context_fn=_ctx, _draft_fn=draft_ok, _selfcheck_fn=sc,
        _safety_pre=_PROCEED, _safety_post=_PROCEED,
        _record_fn=boom_rec, _posts_fn=_fresh_posts_same,
    )
    assert result is not None
