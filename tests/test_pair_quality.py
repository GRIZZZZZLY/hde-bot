"""Tests for the LLM pair-quality gate (Phase 2B)."""
import json

from bot.agent.pair_quality import gate_pending_pairs, judge_pair_quality

_PAIR = {"pair_id": 7, "context": "Клиент: касса не печатает",
         "operator_answer": "Проверьте бумагу и перезапустите кассу",
         "operator_answer_at": "10:00:00 01.03.2026"}


async def test_judge_pair_quality_parses_verdict():
    async def fake_call(system, user, *, model=None, temperature=0.0, max_tokens=600):
        assert "касса не печатает" in user          # контекст в промпте
        assert "Проверьте бумагу" in user           # ответ в промпте
        return json.dumps({"status": "auto_accepted", "reason": "конкретное решение"})

    verdict = await judge_pair_quality(_PAIR, _call_fn=fake_call)
    assert verdict == ("auto_accepted", "конкретное решение")


async def test_judge_pair_quality_rejects_bad_status_and_garbage():
    async def bad_status(system, user, *, model=None, temperature=0.0, max_tokens=600):
        return json.dumps({"status": "great", "reason": "x"})

    async def garbage(system, user, *, model=None, temperature=0.0, max_tokens=600):
        return "не json"

    assert await judge_pair_quality(_PAIR, _call_fn=bad_status) is None
    assert await judge_pair_quality(_PAIR, _call_fn=garbage) is None


async def test_gate_pending_pairs_batch_counts_and_isolation():
    pairs = [dict(_PAIR, pair_id=i) for i in (1, 2, 3)]
    updates = []

    async def fake_list(limit=50, own_operator_id=""):
        return pairs

    async def fake_judge(pair, _call_fn=None):
        if pair["pair_id"] == 1:
            return ("auto_accepted", "ок")
        if pair["pair_id"] == 2:
            return ("rejected", "приветствие без решения")
        return None                                  # rate-limit → skipped

    async def fake_set(pair_id, status, reason):
        updates.append((pair_id, status))

    stats = await gate_pending_pairs(
        _judge_fn=fake_judge, _list_fn=fake_list, _set_fn=fake_set,
        pause_s=0.0, retry_pause_s=0.0,
    )
    assert stats == {"gated": 2, "accepted": 1, "rejected": 1, "outdated": 0, "skipped": 1}
    assert (3, "auto_accepted") not in updates       # skipped не записан


import bot.scheduler as scheduler_module
import bot.config as config_module
import bot.db as db_module


async def test_nightly_job_gates_after_mining(monkeypatch):
    await db_module.init_db()  # dialogue_pairs table must exist before mining runs
    cfg = config_module.config
    monkeypatch.setattr(cfg, "agent_dialogue_mining_enabled", True)
    gate_called = {"limit": None}

    async def fake_gate(limit=50, **k):
        gate_called["limit"] = limit
        return {"gated": 0, "accepted": 0, "rejected": 0, "outdated": 0, "skipped": 0}

    monkeypatch.setattr("bot.agent.pair_quality.gate_pending_pairs", fake_gate)

    class _NoTickets:
        async def get_closed_tickets_page(self, owner_id, page=1):
            return ([], 1)

    monkeypatch.setattr("bot.hde_api.HDEApiClient", lambda: _NoTickets())
    scheduler_module._last_dialogue_backfill_date = None
    # заставить временной гейт пройти: подменяем _now_msk на 01:00
    import datetime as _dt
    fake_now = _dt.datetime(2026, 7, 11, 1, 0, 0)
    monkeypatch.setattr(scheduler_module, "_now_msk", lambda: fake_now)
    await scheduler_module._maybe_backfill_dialogue_pairs(bot=None)
    assert gate_called["limit"] == 300                # гейт вызван после майнинга


async def test_gate_retries_once_after_none():
    """None-вердикт (rate-limit) → пауза и один повтор, не сразу skipped."""
    from bot.agent.pair_quality import gate_pending_pairs

    calls = {"n": 0}

    async def judge(pair):
        calls["n"] += 1
        if calls["n"] == 1:
            return None                      # первый заход — рейт-лимит
        return "auto_accepted", "ok"

    async def list_fn(limit, own_operator_id):
        return [{"pair_id": 1}]

    saved = []

    async def set_fn(pid, status, reason):
        saved.append((pid, status))

    sleeps = []

    async def sleep_fn(s):
        sleeps.append(s)

    stats = await gate_pending_pairs(
        limit=10, _judge_fn=judge, _list_fn=list_fn, _set_fn=set_fn,
        _sleep_fn=sleep_fn, pause_s=0.0, retry_pause_s=30.0,
    )
    assert stats["gated"] == 1 and stats["skipped"] == 0
    assert saved == [(1, "auto_accepted")]
    assert 30.0 in sleeps                    # пауза перед ретраем была


async def test_gate_paces_between_pairs():
    from bot.agent.pair_quality import gate_pending_pairs

    async def judge(pair):
        return "rejected", "мусор"

    async def list_fn(limit, own_operator_id):
        return [{"pair_id": i} for i in (1, 2, 3)]

    async def set_fn(pid, status, reason):
        pass

    sleeps = []

    async def sleep_fn(s):
        sleeps.append(s)

    stats = await gate_pending_pairs(
        limit=10, _judge_fn=judge, _list_fn=list_fn, _set_fn=set_fn,
        _sleep_fn=sleep_fn, pause_s=6.0, retry_pause_s=30.0,
    )
    assert stats["gated"] == 3
    assert sleeps.count(6.0) == 2            # паузы между парами, не после последней
