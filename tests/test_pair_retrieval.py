"""Tests for few-shot pair similarity retrieval (Phase 2B)."""
import numpy as np

import bot.agent.pair_retrieval as pr


def _emb(vec):
    v = np.asarray(vec, dtype=np.float32)
    return (v / np.linalg.norm(v)).tobytes()


def _cand(pid, ticket, op, vec):
    return {"pair_id": pid, "ticket_id": ticket, "operator_user_id": op,
            "context": f"Клиент: вопрос {pid}", "operator_answer": f"ответ {pid}",
            "embedding": _emb(vec)}


_QUERY = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)


async def test_threshold_and_exclusions(monkeypatch):
    pr.invalidate_pairs_cache()

    async def cands():
        return [
            _cand(1, "T1", "98", [1.0, 0.05, 0.0]),   # близкий, свой
            _cand(2, "T2", "98", [0.0, 1.0, 0.0]),    # ниже порога
            _cand(3, "TCUR", "98", [1.0, 0.0, 0.0]),  # текущий тикет — исключён
        ]

    got = await pr.find_similar_pairs(
        _QUERY, exclude_ticket_ids={"TCUR"}, own_operator_id="98",
        _candidates_fn=cands,
    )
    assert [g["pair_id"] for g in got] == [1]
    assert got[0]["score"] >= pr.PAIR_MIN_SCORE


async def test_own_operator_priority_two_pass():
    pr.invalidate_pairs_cache()

    async def cands():
        return [
            _cand(1, "T1", "67", [1.0, 0.0, 0.0]),    # чужой, идеальный матч
            _cand(2, "T2", "98", [1.0, 0.1, 0.0]),    # свой, чуть хуже
            _cand(3, "T3", "98", [1.0, 0.15, 0.0]),   # свой
        ]

    got = await pr.find_similar_pairs(
        _QUERY, limit=2, own_operator_id="98", _candidates_fn=cands,
    )
    ids = [g["pair_id"] for g in got]
    assert ids == [2, 3]                              # свои вытесняют чужой


async def test_others_fill_when_own_insufficient():
    pr.invalidate_pairs_cache()

    async def cands():
        return [
            _cand(1, "T1", "98", [1.0, 0.05, 0.0]),
            _cand(2, "T2", "67", [1.0, 0.02, 0.0]),
        ]

    got = await pr.find_similar_pairs(
        _QUERY, limit=3, own_operator_id="98", _candidates_fn=cands,
    )
    assert [g["pair_id"] for g in got] == [1, 2]      # свой первым, чужой добил


async def test_empty_when_nothing_above_threshold():
    pr.invalidate_pairs_cache()

    async def cands():
        return [_cand(1, "T1", "98", [0.0, 1.0, 0.0])]

    assert await pr.find_similar_pairs(
        _QUERY, own_operator_id="98", _candidates_fn=cands,
    ) == []
