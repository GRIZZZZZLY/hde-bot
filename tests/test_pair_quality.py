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
    )
    assert stats == {"gated": 2, "accepted": 1, "rejected": 1, "outdated": 0, "skipped": 1}
    assert (3, "auto_accepted") not in updates       # skipped не записан
