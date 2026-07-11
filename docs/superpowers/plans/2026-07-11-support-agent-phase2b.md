# Support Agent — Phase 2B (Quality Gating + Dynamic Few-shot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the mined `dialogue_pairs` dataset (~2300+ pairs, growing) into working dynamic few-shot: an LLM quality gate marks pairs usable/unusable, a similarity search finds the operator's own past answers to similar questions, and the agent's draft prompt receives them as examples — behind the existing `AGENT_DYNAMIC_FEWSHOT_ENABLED` flag (off by default).

**Architecture:** Store additions in `bot/db/dialogue_store.py` (gating queue, quality updates, few-shot candidate rows). New `bot/agent/pair_quality.py` (one-call LLM verdict per pair via public `call_groq_json`, rate-limit tolerant batch runner). New `bot/agent/pair_retrieval.py` (numpy cosine over cached candidate embeddings, two-pass own-operator priority, same-ticket + golden exclusions). Integration: `build_agent_context` adds `dialogue_pair` evidence entries (reusing the already-computed query embedding); `generate_agent_draft` renders them as a "похожие решения" block; self-check automatically sees them as grounding evidence (they flow through `evidence`). Nightly gating piggybacks on the existing `_maybe_backfill_dialogue_pairs` job (same `agent_dialogue_mining_enabled` flag). CLI gains `--gate`.

**Tech Stack:** Python 3, aiosqlite, numpy, Groq (`call_groq_json`, model `config.agent_selfcheck_model`), pytest (`asyncio_mode = auto`).

## Global Constraints

- No new third-party dependencies.
- **Flag-gated**: few-shot retrieval runs ONLY when `config.agent_dynamic_fewshot_enabled` (exists since 0A, default False). Flag off → `build_agent_context` byte-identical behavior. Gating (nightly/CLI) runs under `agent_dialogue_mining_enabled` / CLI only.
- **Закрытый тикет ≠ правильный ответ**: only `quality_status IN ('auto_accepted','human_verified')` AND `embedding_status='ready'` pairs are few-shot candidates. `unreviewed` pairs are never used.
- **Порог обязателен**: candidates below `PAIR_MIN_SCORE = 0.83` (same scale as `RAG_MIN_SCORE`, e5 cosine) are NOT included; zero matches → no few-shot block at all (never pad with weak matches).
- **Exclusions**: `pair.ticket_id != current_ticket_id` (future-leak); `exclude_ticket_ids` parameter additionally accepts the golden-set ticket list (consumed by future agent-level eval; runtime passes the current ticket only).
- **Own-operator priority (two-pass)**: pairs with `operator_user_id == config.hde_owner_id` are selected first; only if fewer than `limit` remain do other staff's pairs (still above threshold) fill the rest.
- **LLM gate transport**: public `bot.ai_summary.call_groq_json` with `config.agent_selfcheck_model`; a `None`/invalid reply leaves the pair `unreviewed` (retried next run) — the batch never crashes on rate limits.
- Embedding cache: module-level TTL cache (300s) of candidate rows+matrix, invalidated by the gating writer; memory ~25 MB at 6k pairs — acceptable.
- Store functions in `bot/db/dialogue_store.py`, re-exported from `bot/db/__init__.py`; connection pattern of the file.
- All LLM/embed/db callables injectable via keyword-only `_*_fn` params; tests never hit network/model.
- Tests: `asyncio_mode = auto`; extend `tests/test_dialogue_store.py`, new `tests/test_pair_quality.py`, `tests/test_pair_retrieval.py`, extend `tests/test_agent_context.py`, `tests/test_agent_generate.py`.
- Style: `from __future__ import annotations`, RU docstrings.

## Out of scope

Human-verify кнопка в сводке (optional по спеке — отложена), multi-turn (фаза 3), судья (5B), fine-tune, авто-отправка, включение флагов на проде (отдельное решение после деплоя).

## File Structure

- Modify: `bot/db/dialogue_store.py` — `list_pairs_for_gating`, `set_pair_quality`, `list_fewshot_candidates`, `count_pairs_by_quality`.
- Modify: `bot/db/__init__.py` — re-exports.
- Create: `bot/agent/pair_quality.py` — `judge_pair_quality` (one LLM call), `gate_pending_pairs` (batch).
- Create: `bot/agent/pair_retrieval.py` — `find_similar_pairs` (+TTL cache, `invalidate_pairs_cache`).
- Modify: `bot/agent/context.py` — flag-gated dialogue_pair evidence.
- Modify: `bot/agent/generate.py` — few-shot block in system prompt.
- Modify: `bot/scheduler.py` — nightly gating step inside `_maybe_backfill_dialogue_pairs`.
- Modify: `scripts/backfill_dialogue_pairs.py` — `--gate N`.
- Tests as listed above.

---

## Task 1: Store — gating queue, quality updates, few-shot candidates

**Files:**
- Modify: `bot/db/dialogue_store.py`
- Modify: `bot/db/__init__.py`
- Test: `tests/test_dialogue_store.py` (append)

**Interfaces:**
- Produces:
  - `list_pairs_for_gating(limit: int = 50, *, own_operator_id: str = "") -> list[dict]` — `unreviewed` rows: `pair_id, context, operator_answer, operator_answer_at`. **Own-operator pairs first** (25k+ pairs mined; the operator's own answers are the highest-value few-shot candidates, so they get gated before others' — `ORDER BY (operator_user_id = own) DESC, pair_id`).
  - `set_pair_quality(pair_id: int, status: str, reason: str | None) -> None`.
  - `list_fewshot_candidates() -> list[dict]` — rows with `quality_status IN ('auto_accepted','human_verified') AND embedding_status='ready' AND embedding IS NOT NULL`: `pair_id, ticket_id, operator_user_id, context, operator_answer, embedding`.
  - `count_pairs_by_quality() -> dict[str, int]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_store.py (append)
from bot.db.dialogue_store import (
    count_pairs_by_quality,
    list_fewshot_candidates,
    list_pairs_for_gating,
    set_pair_quality,
)


async def test_gating_queue_and_quality_update():
    await db_module.init_db()
    pid, _ = await save_dialogue_pair(
        ticket_id="T1", context="Клиент: вопрос", operator_answer="ответ",
        content_hash="hq1",
    )
    queue = await list_pairs_for_gating()
    assert any(r["pair_id"] == pid for r in queue)
    await set_pair_quality(pid, "auto_accepted", "полезный ответ")
    assert all(r["pair_id"] != pid for r in await list_pairs_for_gating())
    counts = await count_pairs_by_quality()
    assert counts.get("auto_accepted") == 1


async def test_gating_queue_prioritizes_own_operator():
    await db_module.init_db()
    # чужой ответ вставлен раньше (меньший pair_id), свой — позже
    other, _ = await save_dialogue_pair(
        ticket_id="T1", context="Клиент: a", operator_answer="чужой",
        content_hash="go1", operator_user_id="67",
    )
    own, _ = await save_dialogue_pair(
        ticket_id="T2", context="Клиент: b", operator_answer="мой",
        content_hash="go2", operator_user_id="98",
    )
    queue = await list_pairs_for_gating(own_operator_id="98")
    assert queue[0]["pair_id"] == own          # свой первым, несмотря на pair_id


async def test_fewshot_candidates_require_quality_and_embedding():
    await db_module.init_db()
    ok, _ = await save_dialogue_pair(
        ticket_id="T1", context="Клиент: касса не видит ККТ", operator_answer="проверьте USB",
        content_hash="hf1", operator_user_id="98",
        embedding=b"\x00\x01", embedding_status="ready",
    )
    await set_pair_quality(ok, "auto_accepted", None)
    # unreviewed с эмбеддингом — не кандидат
    await save_dialogue_pair(
        ticket_id="T2", context="Клиент: x", operator_answer="y",
        content_hash="hf2", embedding=b"\x00\x01", embedding_status="ready",
    )
    # accepted без эмбеддинга — не кандидат
    no_emb, _ = await save_dialogue_pair(
        ticket_id="T3", context="Клиент: z", operator_answer="w", content_hash="hf3",
    )
    await set_pair_quality(no_emb, "auto_accepted", None)

    rows = await list_fewshot_candidates()
    assert [r["pair_id"] for r in rows] == [ok]
    assert rows[0]["operator_user_id"] == "98"
    assert rows[0]["embedding"] == b"\x00\x01"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_store.py -k "gating or fewshot_candidates" -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement**

Append to `bot/db/dialogue_store.py`:

```python
async def list_pairs_for_gating(limit: int = 50, *, own_operator_id: str = "") -> list[dict]:
    """Очередь LLM-фильтра качества: непроверенные пары (Phase 2B).
    Ответы own_operator_id гейтятся первыми — самые ценные few-shot-примеры
    (в датасете 25k+ пар, полный прогон дорог; свои важнее чужих)."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pair_id, context, operator_answer, operator_answer_at "
            "FROM dialogue_pairs WHERE quality_status='unreviewed' "
            "ORDER BY (operator_user_id = ?) DESC, pair_id LIMIT ?",
            (str(own_operator_id), limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_pair_quality(pair_id: int, status: str, reason: str | None) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE dialogue_pairs SET quality_status=?, quality_reason=? "
            "WHERE pair_id=?",
            (status, reason, pair_id),
        )
        await db.commit()


async def list_fewshot_candidates() -> list[dict]:
    """Кандидаты dynamic few-shot: качество подтверждено, эмбеддинг готов."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pair_id, ticket_id, operator_user_id, context, "
            "operator_answer, embedding "
            "FROM dialogue_pairs "
            "WHERE quality_status IN ('auto_accepted','human_verified') "
            "AND embedding_status='ready' AND embedding IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_pairs_by_quality() -> dict:
    async with connect() as db:
        async with db.execute(
            "SELECT quality_status, COUNT(*) FROM dialogue_pairs GROUP BY quality_status"
        ) as cur:
            rows = await cur.fetchall()
    return {r[0]: int(r[1]) for r in rows}
```

Add the four names to the `from .dialogue_store import (...)` block in `bot/db/__init__.py` (alphabetical).

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_dialogue_store.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/db/dialogue_store.py bot/db/__init__.py tests/test_dialogue_store.py
git commit -m "feat(fewshot): store — gating queue, quality updates, candidates"
```

---

## Task 2: LLM quality gate

**Files:**
- Create: `bot/agent/pair_quality.py`
- Test: `tests/test_pair_quality.py`

**Interfaces:**
- Consumes: `bot.ai_summary.call_groq_json`, `config.agent_selfcheck_model`, store (Task 1).
- Produces:
  - `judge_pair_quality(pair: dict, *, _call_fn=None) -> tuple[str, str] | None` — `(status, reason)`; status ∈ `auto_accepted | rejected | outdated`; None on unparseable/failed call (pair stays unreviewed).
  - `gate_pending_pairs(limit: int = 50, *, _judge_fn=None, _list_fn=None, _set_fn=None) -> dict` — batch: returns `{"gated": n, "accepted": a, "rejected": r, "outdated": o, "skipped": s}`; a None verdict counts as skipped and does NOT stop the batch; calls `invalidate_pairs_cache()` from Task 3 lazily (try/except ImportError during early tasks is unnecessary — Task 3 lands before wiring; in this task call the store setter only, the cache invalidation is added in Task 3's step).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pair_quality.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pair_quality.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# bot/agent/pair_quality.py
"""LLM-фильтр качества dialogue_pairs (Phase 2B).

Закрытый тикет ≠ правильный ответ: в few-shot идут только пары, прошедшие
фильтр (auto_accepted). Отсев: приветствия/служебные без решения, «позвоните
нам», промежуточные реплики, устаревшие инструкции. None-вердикт (rate-limit,
мусорный JSON) оставляет пару unreviewed — доберётся следующим прогоном."""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_STATUSES = ("auto_accepted", "rejected", "outdated")


def _build_gate_prompt(pair: dict) -> tuple[str, str]:
    system = (
        "Ты оцениваешь, годится ли ответ оператора техподдержки кассового ПО "
        "как ОБРАЗЕЦ для обучения ассистента (few-shot пример).\n"
        "auto_accepted — конкретное решение/уточнение по существу: есть действие, "
        "модель/ПО, путь или конкретный вопрос.\n"
        "rejected — приветствие, служебная реплика, «позвоните нам» без решения, "
        "«ожидайте», пустое подтверждение, фрагмент без смысла.\n"
        "outdated — инструкция, явно привязанная к устаревшей версии/процессу.\n"
        'Верни СТРОГО JSON: {"status":"auto_accepted|rejected|outdated",'
        '"reason":"кратко по-русски"}'
    )
    user = (
        f"Диалог до ответа:\n{pair['context'][-1500:]}\n\n"
        f"Ответ оператора (кандидат в образцы):\n{pair['operator_answer'][:800]}\n\n"
        f"Дата ответа: {pair.get('operator_answer_at') or 'неизвестна'}"
    )
    return system, user


async def judge_pair_quality(pair: dict, *, _call_fn=None) -> tuple[str, str] | None:
    if _call_fn is None:
        from ..ai_summary import call_groq_json as _call_fn
    from ..config import config

    system, user = _build_gate_prompt(pair)
    try:
        raw = await _call_fn(system, user, model=config.agent_selfcheck_model)
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("status") not in _STATUSES:
        return None
    return obj["status"], str(obj.get("reason", ""))[:200]


async def gate_pending_pairs(
    limit: int = 50, *, _judge_fn=None, _list_fn=None, _set_fn=None,
) -> dict:
    """Батч-фильтр: размечает до limit непроверенных пар. Не падает на сбоях."""
    if _judge_fn is None:
        _judge_fn = judge_pair_quality
    if _list_fn is None:
        from ..db import list_pairs_for_gating as _list_fn
    if _set_fn is None:
        from ..db import set_pair_quality as _set_fn
    from ..config import config

    stats = {"gated": 0, "accepted": 0, "rejected": 0, "outdated": 0, "skipped": 0}
    for pair in await _list_fn(limit, own_operator_id=str(config.hde_owner_id)):
        verdict = await _judge_fn(pair)
        if verdict is None:
            stats["skipped"] += 1
            continue
        status, reason = verdict
        await _set_fn(pair["pair_id"], status, reason)
        stats["gated"] += 1
        key = {"auto_accepted": "accepted", "rejected": "rejected",
               "outdated": "outdated"}[status]
        stats[key] += 1
    if stats["gated"]:
        try:
            from .pair_retrieval import invalidate_pairs_cache
            invalidate_pairs_cache()
        except ImportError:
            pass  # Task 3 ещё не в дереве — безопасно при поэтапной сборке
    return stats
```

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_pair_quality.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/agent/pair_quality.py tests/test_pair_quality.py
git commit -m "feat(fewshot): LLM pair-quality gate with fault-tolerant batch"
```

---

## Task 3: Similarity retrieval with own-operator priority

**Files:**
- Create: `bot/agent/pair_retrieval.py`
- Test: `tests/test_pair_retrieval.py`

**Interfaces:**
- Consumes: `list_fewshot_candidates` (Task 1), numpy.
- Produces:
  - `PAIR_MIN_SCORE = 0.83`
  - `invalidate_pairs_cache() -> None`
  - `find_similar_pairs(query_embedding, *, limit=3, exclude_ticket_ids=frozenset(), own_operator_id="", _candidates_fn=None) -> list[dict]` — each: `{pair_id, ticket_id, operator_user_id, context, operator_answer, score}`; cosine ≥ threshold; excluded tickets dropped; two-pass: own operator first, others fill the remainder; embeddings deserialized via `np.frombuffer(..., dtype=np.float32)`; module TTL cache (300s) of candidate rows.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pair_retrieval.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pair_retrieval.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# bot/agent/pair_retrieval.py
"""Поиск похожих dialogue_pairs для dynamic few-shot (Phase 2B).

Косинус по problem-side эмбеддингам; порог обязателен (слабые совпадения не
подмешиваются); пары текущего тикета/golden set исключаются; двухпроходный
приоритет собственных ответов оператора (учимся у НЕГО, чужие — fallback)."""
from __future__ import annotations

import time

import numpy as np

PAIR_MIN_SCORE = 0.83
_CACHE_TTL = 300.0

_cache_rows: list[dict] | None = None
_cache_at: float = 0.0


def invalidate_pairs_cache() -> None:
    global _cache_rows, _cache_at
    _cache_rows = None
    _cache_at = 0.0


async def _load_candidates(_candidates_fn):
    global _cache_rows, _cache_at
    now = time.monotonic()
    if _cache_rows is not None and now - _cache_at < _CACHE_TTL:
        return _cache_rows
    if _candidates_fn is None:
        from ..db import list_fewshot_candidates as _candidates_fn
    _cache_rows = await _candidates_fn()
    _cache_at = now
    return _cache_rows


async def find_similar_pairs(
    query_embedding,
    *,
    limit: int = 3,
    exclude_ticket_ids=frozenset(),
    own_operator_id: str = "",
    _candidates_fn=None,
) -> list[dict]:
    rows = await _load_candidates(_candidates_fn)
    if not rows:
        return []
    query = np.asarray(query_embedding, dtype=np.float32)
    qn = np.linalg.norm(query)
    if not qn:
        return []
    query = query / qn
    excluded = {str(t) for t in exclude_ticket_ids}

    scored: list[dict] = []
    for row in rows:
        if str(row["ticket_id"]) in excluded:
            continue
        emb = np.frombuffer(row["embedding"], dtype=np.float32)
        if emb.shape != query.shape:
            continue
        score = float(np.dot(emb, query))  # кандидаты нормализованы при эмбеддинге
        if score < PAIR_MIN_SCORE:
            continue
        scored.append({
            "pair_id": row["pair_id"], "ticket_id": row["ticket_id"],
            "operator_user_id": row["operator_user_id"],
            "context": row["context"], "operator_answer": row["operator_answer"],
            "score": round(score, 4),
        })
    scored.sort(key=lambda r: r["score"], reverse=True)

    own = [r for r in scored if str(r["operator_user_id"]) == str(own_operator_id)]
    others = [r for r in scored if str(r["operator_user_id"]) != str(own_operator_id)]
    return (own + others)[:limit]
```

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_pair_retrieval.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/agent/pair_retrieval.py tests/test_pair_retrieval.py
git commit -m "feat(fewshot): cosine pair retrieval, own-operator two-pass priority"
```

---

## Task 4: ContextBuilder integration (flag-gated evidence)

**Files:**
- Modify: `bot/agent/context.py`
- Test: `tests/test_agent_context.py` (append)

**Interfaces:**
- Produces: `build_agent_context(...)` gains `ticket_id: str = ""` and `_pairs_fn=None` params. When `config.agent_dynamic_fewshot_enabled` AND the query embedding is available: `find_similar_pairs(embedding, limit=3, exclude_ticket_ids={ticket_id}, own_operator_id=config.hde_owner_id)`; each hit appended to `evidence` as `{"source_type": "dialogue_pair", "source_id": pair_id, "rank": ..., "score": ..., "title": f"тикет {ticket_id}", "used_excerpt": "Вопрос: <last client line>\nОтвет оператора: <answer[:400]>"}` and to `grounds` as `пара#<id>`. Flag off → no calls, no evidence change. The SAME query embedding computed for KB search is reused (no second embed call).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_context.py (append)
import bot.config as config_module


async def test_fewshot_evidence_added_when_flag_on(monkeypatch):
    posts = _mk_posts()
    info = SimpleNamespace(client_id=1)
    monkeypatch.setattr(config_module.config, "agent_dynamic_fewshot_enabled", True)
    monkeypatch.setattr(config_module.config, "hde_owner_id", "98")

    async def fake_embed(text, task_type="query"):
        import numpy as np
        return np.ones(4, dtype=np.float32)

    async def empty_similar(emb, *, limit=3, query_text="", company_id=""):
        return []

    async def none_wiki(title):
        return None

    async def none_pattern(equipment, keywords):
        return None

    captured = {}

    async def fake_pairs(emb, *, limit=3, exclude_ticket_ids=frozenset(),
                         own_operator_id=""):
        captured["exclude"] = set(exclude_ticket_ids)
        captured["own"] = own_operator_id
        return [{"pair_id": 5, "ticket_id": "T9", "operator_user_id": "98",
                 "context": "Клиент: похожий вопрос",
                 "operator_answer": "мой прошлый ответ", "score": 0.9}]

    ctx = await build_agent_context(
        posts, info, "Не печатает чек", ticket_id="TCUR",
        _history_fn=lambda p, i: "H", _embed_fn=fake_embed, _similar_fn=empty_similar,
        _equipment_fn=lambda t, h: None, _wiki_fn=none_wiki, _pattern_fn=none_pattern,
        _pairs_fn=fake_pairs,
    )
    pair_ev = [e for e in ctx["evidence"] if e["source_type"] == "dialogue_pair"]
    assert len(pair_ev) == 1
    assert "мой прошлый ответ" in pair_ev[0]["used_excerpt"]
    assert captured["exclude"] == {"TCUR"}            # same-ticket исключён
    assert captured["own"] == "98"
    assert any(g == "пара#5" for g in ctx["grounds"])


async def test_fewshot_skipped_when_flag_off(monkeypatch):
    posts = _mk_posts()
    info = SimpleNamespace(client_id=1)
    monkeypatch.setattr(config_module.config, "agent_dynamic_fewshot_enabled", False)
    called = {"v": False}

    async def fake_embed(text, task_type="query"):
        import numpy as np
        return np.ones(4, dtype=np.float32)

    async def empty_similar(emb, *, limit=3, query_text="", company_id=""):
        return []

    async def none_wiki(title):
        return None

    async def none_pattern(equipment, keywords):
        return None

    async def spy_pairs(*a, **k):
        called["v"] = True
        return []

    ctx = await build_agent_context(
        posts, info, "t", ticket_id="TCUR",
        _history_fn=lambda p, i: "H", _embed_fn=fake_embed, _similar_fn=empty_similar,
        _equipment_fn=lambda t, h: None, _wiki_fn=none_wiki, _pattern_fn=none_pattern,
        _pairs_fn=spy_pairs,
    )
    assert called["v"] is False                       # флаг off → ни вызова
    assert all(e["source_type"] != "dialogue_pair" for e in ctx["evidence"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_context.py -k fewshot -v`
Expected: FAIL — unexpected kwargs.

- [ ] **Step 3: Implement**

In `bot/agent/context.py::build_agent_context`: add params `ticket_id: str = ""` (positional-friendly keyword after `company_id`) and `_pairs_fn=None`. After the KB/wiki/pattern evidence block (before `grounds` derivation), insert:

```python
    from ..config import config
    if config.agent_dynamic_fewshot_enabled and embedding is not None:
        if _pairs_fn is None:
            from .pair_retrieval import find_similar_pairs as _pairs_fn
        try:
            pair_hits = await _pairs_fn(
                embedding, limit=3,
                exclude_ticket_ids={str(ticket_id)} if ticket_id else frozenset(),
                own_operator_id=str(config.hde_owner_id),
            )
        except Exception:
            pair_hits = []  # few-shot не должен ронять генерацию
        for hit in pair_hits:
            client_lines = [
                ln for ln in hit["context"].splitlines() if ln.startswith("Клиент:")
            ]
            last_client = client_lines[-1][len("Клиент:"):].strip() if client_lines else ""
            evidence.append({
                "source_type": "dialogue_pair",
                "source_id": hit["pair_id"],
                "rank": len(evidence) + 1,
                "score": hit["score"],
                "title": f"тикет {hit['ticket_id']}",
                "used_excerpt": (
                    f"Вопрос: {last_client}\n"
                    f"Ответ оператора: {hit['operator_answer'][:400]}"
                ),
            })
```

And in the `grounds` loop add a branch:

```python
        elif e["source_type"] == "dialogue_pair":
            grounds.append(f"пара#{e['source_id']}")
```

Thread `ticket_id` from `run_agent` (`bot/agent/pipeline.py` passes `ticket_id=ticket_id` into `_context_fn`) — one-line change in `pipeline.py` where `_context_fn(posts, info, ticket_title, company_id)` is called: add `ticket_id=ticket_id`. Note: `_context_fn` custom fakes in existing pipeline tests accept `*a, **k` — verify and adjust the fakes' signatures if they are strict (the file's `_ctx(*a, **k)` already tolerates it).

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_agent_context.py tests/test_agent_pipeline.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/agent/context.py bot/agent/pipeline.py tests/test_agent_context.py
git commit -m "feat(fewshot): flag-gated dialogue_pair evidence in agent context"
```

---

## Task 5: Draft prompt — few-shot block

**Files:**
- Modify: `bot/agent/generate.py`
- Test: `tests/test_agent_generate.py` (append)

**Interfaces:**
- Produces: `generate_agent_draft` renders dialogue_pair evidence as an extra system-prompt block appended after `build_action_instruction()`:

```
Примеры, как оператор решал похожие обращения (следуй их стилю и конкретике):
Пример 1:
Вопрос: ...
Ответ оператора: ...
```

No block when there are no dialogue_pair entries. KB evidence continues to flow as `rag_examples` (unchanged).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_generate.py (append)
_CTX_PAIRS = dict(_CTX)
_CTX_PAIRS["evidence"] = _CTX["evidence"] + [
    {"source_type": "dialogue_pair", "source_id": 5, "rank": 2, "score": 0.9,
     "title": "тикет T9",
     "used_excerpt": "Вопрос: похожий вопрос\nОтвет оператора: мой прошлый ответ"},
]


async def test_fewshot_block_appended_to_system():
    seen = {}

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        seen["system"] = system
        return '{"action":"ANSWER","suit":"с","client":"к","memo":"м","confidence":80}'

    async def fake_format():
        return "F"

    await generate_agent_draft(
        _CTX_PAIRS, "t",
        _call_fn=fake_call, _prompt_fn=lambda *a, **k: "BASE", _format_fn=fake_format,
    )
    assert "похожие обращения" in seen["system"]
    assert "мой прошлый ответ" in seen["system"]


async def test_no_fewshot_block_without_pairs():
    seen = {}

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        seen["system"] = system
        return '{"action":"ANSWER","suit":"с","client":"к","memo":"м","confidence":80}'

    async def fake_format():
        return "F"

    await generate_agent_draft(
        _CTX, "t",
        _call_fn=fake_call, _prompt_fn=lambda *a, **k: "BASE", _format_fn=fake_format,
    )
    assert "похожие обращения" not in seen["system"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_generate.py -k fewshot -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `bot/agent/generate.py`, after `system = base_system + build_action_instruction()` add:

```python
    pair_examples = [
        e["used_excerpt"] for e in context.get("evidence", [])
        if e.get("source_type") == "dialogue_pair"
    ]
    if pair_examples:
        block = "\n\nПримеры, как оператор решал похожие обращения (следуй их стилю и конкретике):"
        for i, ex in enumerate(pair_examples, 1):
            block += f"\nПример {i}:\n{ex}"
        system += block
```

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_agent_generate.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/agent/generate.py tests/test_agent_generate.py
git commit -m "feat(fewshot): render pair examples in draft system prompt"
```

---

## Task 6: Nightly gating + CLI `--gate`

**Files:**
- Modify: `bot/scheduler.py` (inside `_maybe_backfill_dialogue_pairs`)
- Modify: `scripts/backfill_dialogue_pairs.py`
- Test: `tests/test_pair_quality.py` (append)

**Interfaces:**
- Scheduler: after the mining loop in `_maybe_backfill_dialogue_pairs` (same flag/time gate), run `gate_pending_pairs(limit=50)` and log the stats — pairs mined tonight get judged the same night, throttled by the batch limit.
- CLI: `--gate N` runs ONLY the gating batch (`gate_pending_pairs(limit=N)`) and prints stats + `count_pairs_by_quality()`; `--status` additionally prints the quality breakdown.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pair_quality.py (append)
import bot.scheduler as scheduler_module
import bot.config as config_module


async def test_nightly_job_gates_after_mining(monkeypatch):
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
    assert gate_called["limit"] == 50                 # гейт вызван после майнинга
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pair_quality.py -k nightly -v`
Expected: FAIL — gate not called.

- [ ] **Step 3: Implement**

In `bot/scheduler.py::_maybe_backfill_dialogue_pairs`, after `logger.info("Nightly dialogue backfill: +%d pairs", saved)` (inside the same try), add:

```python
        from .agent.pair_quality import gate_pending_pairs
        gate_stats = await gate_pending_pairs(limit=50)
        logger.info("Nightly pair gating: %s", gate_stats)
```

In `scripts/backfill_dialogue_pairs.py`: add `parser.add_argument("--gate", type=int, default=None, help="только LLM-фильтр качества: разметить N пар")`; in `main()` dispatch before the others:

```python
    if args.gate is not None:
        asyncio.run(_gate(args.gate))
```

with:

```python
async def _gate(limit: int) -> None:
    import bot.db as db
    from bot.agent.pair_quality import gate_pending_pairs
    await db.init_db()
    stats = await gate_pending_pairs(limit=limit)
    print(f"gate: {stats}")
    print(f"quality: {await db.count_pairs_by_quality()}")
```

And extend `_status()` to also print `await db.count_pairs_by_quality()`.

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_pair_quality.py -v`
Smoke: `python scripts/backfill_dialogue_pairs.py --help` → exit 0 with `--gate`.

- [ ] **Step 5: Commit**

```bash
git add bot/scheduler.py scripts/backfill_dialogue_pairs.py tests/test_pair_quality.py
git commit -m "feat(fewshot): nightly gating after mining + CLI --gate"
```

---

## Task 7: Full-suite verification

- [ ] **Step 1:** `python -m pytest -q` → PASS, no regressions.
- [ ] **Step 2:** `python -c "import bot.main" 2>&1 | tail -1` → clean import.
- [ ] **Step 3:** Default-off: `python -c "import bot.config as c; print(c.config.agent_dynamic_fewshot_enabled)"` → `False`.
- [ ] **Step 4:** `git commit -m "test(fewshot): phase 2B verification" --allow-empty`.

---

## Self-Review

**Spec coverage (Phase 2B + rev.3):**
- LLM-фильтр качества, отсев приветствий/служебных/устаревших, quality_status+reason → Tasks 1–2. ✓
- «Закрытый тикет ≠ правильный ответ» — few-shot только из auto_accepted/human_verified → Tasks 1, 3 (candidate filter). ✓
- «До N выше порога качества и сходства; нет — без few-shot» → Task 3 (threshold + empty result), Task 5 (no block without pairs). ✓
- Same-ticket запрет + golden-исключение (параметром) → Task 3 (`exclude_ticket_ids`), Task 4 (runtime passes current ticket). ✓
- Подключение к пайплайну фазы 1 за `agent_dynamic_fewshot_enabled` → Task 4 (flag off = байт-в-байт). ✓
- Own-operator приоритет (rev.3 «учиться на ЭТОМ операторе», operator_user_id) → Task 3 two-pass. ✓
- Rate-limit устойчивость гейта (free-tier TPM) → Task 2 (None → skipped, не крэш; лимит 50/ночь). ✓
- Human-verify кнопка — отложена (Out of scope, спека помечает её optional). ✓

**Placeholder scan:** clean; Task 4's note about pipeline fakes instructs verification of existing test tolerance, with the concrete edit named.

**Type consistency:** `list_fewshot_candidates` row keys (Task 1) match `find_similar_pairs` reads (Task 3); hit dict keys match ContextBuilder usage (Task 4); evidence entry shape matches Task 5's filter and the existing selfcheck `_render_evidence` (source_type + used_excerpt only); `gate_pending_pairs` stats keys match Task 6's scheduler log and CLI print; `invalidate_pairs_cache` defined in Task 3, lazily imported in Task 2 (ImportError-safe until Task 3 lands).
