# Support Agent — Phase 2A (Retro Backfill of dialogue_pairs) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `dialogue_pairs` table + its store, slice every closed HDE ticket into multi-turn "context-before-operator-answer → operator-answer" pairs (local code, no LLM, no future-leak), embed each pair with e5-large, and backfill all closed tickets (~1000–5000) resumably via a CLI checkpoint. This is the dataset Phase 2B quality-gates and Phase 1's dynamic few-shot draws from.

**Architecture:** New module `bot/agent/dialogue_mining.py` (pure slicing + embed/save, injectable HDE client and embed fn) + store functions in a new `bot/db/dialogue_store.py` (re-exported from `bot/db/__init__.py`) + a thin resumable CLI `scripts/backfill_dialogue_pairs.py`. Reuses the e5-large embedder from `bot/knowledge/indexer.embed_text` and the resumable-checkpoint pattern of `/aiimport` (`set_setting`/`get_setting`). All pairs are created `quality_status='unreviewed'`; quality filtering is Phase 2B.

**Tech Stack:** Python 3, aiosqlite, numpy (already a dep via knowledge store), pytest (`asyncio_mode = auto`).

## Global Constraints

- No new third-party dependencies (stdlib + already-present aiosqlite/numpy).
- **No bot-runtime behavior change**: mining runs only from the CLI and (optional) a gated nightly job; the live message flow is untouched. The nightly increment job (Task 6) is gated on `config.agent_enabled` (default off) so it does nothing until explicitly enabled.
- **No future-leak**: a pair's `context` is built strictly from posts BEFORE the operator's answer (`posts[:staff_idx]`); `operator_answer = posts[staff_idx]`; comments excluded before indexing. This is the single most important correctness property — every slicing test asserts it.
- Store functions live in `bot/db/dialogue_store.py`, re-exported from `bot/db/__init__.py`; match the connection pattern of `bot/db/optimizer_store.py` (`from .core import connect`, `async with connect() as db`, `db.row_factory = aiosqlite.Row`, `await db.commit()`).
- Table created in `bot/db/core.py::init_db` with `CREATE TABLE IF NOT EXISTS` (same placement pattern as the Phase 0A tables already there).
- `save_dialogue_pair` is idempotent on `content_hash` (`INSERT OR IGNORE` + `SELECT`), like `record_suggestion`.
- Staff detection: `user_id` in a configurable staff-id set, else falls back to `owner_id`; bot/system posts excluded. Staff ids come from a new env `AGENT_STAFF_USER_IDS` (CSV via existing `config._parse_csv`), defaulting to `(hde_owner_id,)`.
- Embedding: reuse `bot.knowledge.indexer.embed_text(text, task_type="passage")` (e5-large, 1024-dim); store the model name string `"intfloat/multilingual-e5-large"` in `embedding_model`.
- Pair schema keys exactly as in the spec: `pair_id, ticket_id, source_message_id, context_until_message_id, operator_message_id, context, operator_answer, issue_type, client_id, created_at, resolved_at, resolution_status, quality_status, quality_reason, embedding, embedding_model, content_hash`.
- Tests: `asyncio_mode = auto` (plain `async def`), new file `tests/test_dialogue_mining.py`, autouse `set_test_db` isolates the DB.
- Match existing style: `from __future__ import annotations`, RU docstrings/comments.

## Out of scope

Quality gating / `quality_status` transitions beyond `unreviewed` (Phase 2B), dynamic few-shot retrieval and same-ticket/golden exclusion at query time (Phase 1/2B), the "closed + not reopened 7 days" wait (Phase 5B judge concern; 2A stores pairs from `closed` tickets and 2B filters quality).

## File Structure

- Modify: `bot/db/core.py` — add `dialogue_pairs` CREATE TABLE + indexes in `init_db`.
- Create: `bot/db/dialogue_store.py` — `save_dialogue_pair`, `count_dialogue_pairs`, `dialogue_pair_hashes`, checkpoint get/set helpers.
- Modify: `bot/db/__init__.py` — re-export new store functions.
- Modify: `bot/config.py` — add `agent_staff_user_ids` field.
- Create: `bot/agent/dialogue_mining.py` — `_strip_html`, `_pair_content_hash`, `split_ticket_into_pairs`, `mine_ticket_pairs` (embed+save one ticket), `staff_id_set`.
- Create: `scripts/backfill_dialogue_pairs.py` — resumable CLI over `get_closed_tickets_page`.
- Modify: `bot/scheduler.py` — gated nightly increment job.
- Test: `tests/test_dialogue_mining.py`, `tests/test_dialogue_store.py`.

## Human workflow after implementation

1. `python scripts/backfill_dialogue_pairs.py --pages 20` → mines up to 20 pages, saves checkpoint; re-run to continue.
2. Repeat across nights until `--status` reports pages exhausted.
3. Phase 2B later filters quality; Phase 1 draws few-shot once 2B lands.

---

## Task 1: Schema — `dialogue_pairs` table

**Files:**
- Modify: `bot/db/core.py` (inside `init_db`, before the final `await db.commit()`)
- Test: `tests/test_dialogue_store.py`

**Interfaces:**
- Produces: table `dialogue_pairs` with all spec columns + indexes on `content_hash` (unique) and `quality_status`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_store.py
import bot.db as db_module


async def _columns(table: str) -> set[str]:
    from bot.db.core import connect
    async with connect() as db:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
    return {r[1] for r in rows}


async def test_dialogue_pairs_table_created():
    await db_module.init_db()
    cols = await _columns("dialogue_pairs")
    expected = {
        "pair_id", "ticket_id", "source_message_id", "context_until_message_id",
        "operator_message_id", "context", "operator_answer", "issue_type",
        "client_id", "created_at", "resolved_at", "resolution_status",
        "quality_status", "quality_reason", "embedding", "embedding_model",
        "content_hash",
    }
    assert expected <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_store.py -v`
Expected: FAIL — `no such table: dialogue_pairs`.

- [ ] **Step 3: Add the table in `init_db`**

In `bot/db/core.py`, immediately before the closing `await db.commit()` of `init_db`, insert:

```python
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS dialogue_pairs (
                pair_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id                 TEXT NOT NULL,
                source_message_id         TEXT,
                context_until_message_id  TEXT,
                operator_message_id       TEXT,
                context                   TEXT NOT NULL,
                operator_answer           TEXT NOT NULL,
                issue_type                TEXT,
                client_id                 TEXT,
                created_at                TEXT NOT NULL DEFAULT (datetime('now')),
                resolved_at               TEXT,
                resolution_status         TEXT,
                quality_status            TEXT NOT NULL DEFAULT 'unreviewed',
                quality_reason            TEXT,
                embedding                 BLOB,
                embedding_model           TEXT,
                content_hash              TEXT UNIQUE
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dialogue_pairs_quality "
            "ON dialogue_pairs(quality_status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dialogue_pairs_ticket "
            "ON dialogue_pairs(ticket_id)"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dialogue_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/db/core.py tests/test_dialogue_store.py
git commit -m "feat(dialogue): dialogue_pairs schema"
```

---

## Task 2: Store — save (idempotent), count, hashes, checkpoint

**Files:**
- Create: `bot/db/dialogue_store.py`
- Modify: `bot/db/__init__.py`
- Test: `tests/test_dialogue_store.py` (append)

**Interfaces:**
- Produces:
  - `save_dialogue_pair(*, ticket_id, context, operator_answer, content_hash, source_message_id=None, context_until_message_id=None, operator_message_id=None, issue_type=None, client_id=None, resolution_status=None, embedding=None, embedding_model=None) -> int` — idempotent on `content_hash`, returns pair_id (existing on repeat).
  - `count_dialogue_pairs() -> int`
  - `dialogue_pair_hashes() -> set[str]`
  - `get_backfill_checkpoint() -> int` (last completed page, 0 if none)
  - `set_backfill_checkpoint(page: int) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_store.py (append)
from bot.db.dialogue_store import (
    count_dialogue_pairs,
    dialogue_pair_hashes,
    get_backfill_checkpoint,
    save_dialogue_pair,
    set_backfill_checkpoint,
)


async def test_save_is_idempotent_on_hash():
    await db_module.init_db()
    kw = dict(ticket_id="T1", context="Клиент: вопрос",
              operator_answer="ответ", content_hash="h1")
    first = await save_dialogue_pair(**kw)
    again = await save_dialogue_pair(**kw)
    assert first == again
    assert await count_dialogue_pairs() == 1
    assert "h1" in await dialogue_pair_hashes()


async def test_backfill_checkpoint_roundtrip():
    await db_module.init_db()
    assert await get_backfill_checkpoint() == 0
    await set_backfill_checkpoint(7)
    assert await get_backfill_checkpoint() == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_store.py -k "idempotent or checkpoint" -v`
Expected: FAIL — `No module named 'bot.db.dialogue_store'`.

- [ ] **Step 3: Create the store**

```python
# bot/db/dialogue_store.py
"""Store for dialogue_pairs (Phase 2A retro backfill dataset)."""
from __future__ import annotations

import aiosqlite

from .core import connect
from .misc import get_setting, set_setting

_CHECKPOINT_KEY = "dialogue_backfill_page"


async def save_dialogue_pair(
    *,
    ticket_id: str,
    context: str,
    operator_answer: str,
    content_hash: str,
    source_message_id: str | None = None,
    context_until_message_id: str | None = None,
    operator_message_id: str | None = None,
    issue_type: str | None = None,
    client_id: str | None = None,
    resolution_status: str | None = None,
    embedding: bytes | None = None,
    embedding_model: str | None = None,
) -> int:
    """Вставляет пару (идемпотентно по content_hash). Возвращает pair_id."""
    async with connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO dialogue_pairs "
            "(ticket_id, source_message_id, context_until_message_id, "
            " operator_message_id, context, operator_answer, issue_type, "
            " client_id, resolution_status, embedding, embedding_model, content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_id, source_message_id, context_until_message_id,
                operator_message_id, context, operator_answer, issue_type,
                client_id, resolution_status, embedding, embedding_model, content_hash,
            ),
        )
        await db.commit()
        async with db.execute(
            "SELECT pair_id FROM dialogue_pairs WHERE content_hash=?", (content_hash,)
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def count_dialogue_pairs() -> int:
    async with connect() as db:
        async with db.execute("SELECT COUNT(*) FROM dialogue_pairs") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def dialogue_pair_hashes() -> set[str]:
    async with connect() as db:
        async with db.execute(
            "SELECT content_hash FROM dialogue_pairs WHERE content_hash IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def get_backfill_checkpoint() -> int:
    raw = await get_setting(_CHECKPOINT_KEY, "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def set_backfill_checkpoint(page: int) -> None:
    await set_setting(_CHECKPOINT_KEY, str(page))
```

Add to `bot/db/__init__.py` after the `suggestion_store` import block:

```python
from .dialogue_store import (
    count_dialogue_pairs,
    dialogue_pair_hashes,
    get_backfill_checkpoint,
    save_dialogue_pair,
    set_backfill_checkpoint,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dialogue_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/db/dialogue_store.py bot/db/__init__.py tests/test_dialogue_store.py
git commit -m "feat(dialogue): store — idempotent save, count, hashes, checkpoint"
```

---

## Task 3: Config — staff user ids

**Files:**
- Modify: `bot/config.py`
- Test: `tests/test_dialogue_mining.py`

**Interfaces:**
- Produces: `config.agent_staff_user_ids: tuple[str, ...]` — from env `AGENT_STAFF_USER_IDS` (CSV), empty tuple if unset.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_mining.py
"""Tests for Phase 2A dialogue-pair mining."""
import bot.config as config_module


def test_agent_staff_user_ids_parsed(monkeypatch):
    monkeypatch.setenv("AGENT_STAFF_USER_IDS", "10, 20 ,30")
    fresh = config_module.Config.from_env()
    assert fresh.agent_staff_user_ids == ("10", "20", "30")


def test_agent_staff_user_ids_empty_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_STAFF_USER_IDS", raising=False)
    fresh = config_module.Config.from_env()
    assert fresh.agent_staff_user_ids == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_mining.py -k staff_user_ids -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'agent_staff_user_ids'`.

- [ ] **Step 3: Add the field**

In `bot/config.py`, add to the `Config` dataclass after `agent_call_fixation_enabled: bool`:

```python
    agent_staff_user_ids: tuple[str, ...]
```

In `from_env`, add after the `agent_call_fixation_enabled=...` entry:

```python
            agent_staff_user_ids=_parse_csv(os.getenv("AGENT_STAFF_USER_IDS")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dialogue_mining.py -k staff_user_ids -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/config.py tests/test_dialogue_mining.py
git commit -m "feat(dialogue): AGENT_STAFF_USER_IDS config"
```

---

## Task 4: Slicing — split_ticket_into_pairs (no future-leak)

**Files:**
- Create: `bot/agent/dialogue_mining.py`
- Test: `tests/test_dialogue_mining.py` (append)

**Interfaces:**
- Consumes: post objects with `.post_id`, `.user_id`, `.text`, `.is_comment` (HDEPost shape).
- Produces:
  - `_strip_html(text: str) -> str`
  - `staff_id_set(owner_id: str, staff_ids: tuple[str, ...]) -> set[str]` — staff_ids if non-empty else `{owner_id}`.
  - `_pair_content_hash(ticket_id: str, operator_message_id: str, context: str) -> str`
  - `split_ticket_into_pairs(ticket_id, posts, staff: set[str]) -> list[dict]` — one dict per staff answer that has client context before it: `{ticket_id, source_message_id, context_until_message_id, operator_message_id, context, operator_answer, content_hash}`. Comments excluded; `context` = all non-staff posts strictly before the staff post (joined "Клиент: ..."); staff answers with no preceding client text are skipped. **Every staff turn** yields a pair (multi-turn), each with context up to that turn only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_mining.py (append)
from types import SimpleNamespace

from bot.agent.dialogue_mining import (
    _strip_html,
    split_ticket_into_pairs,
    staff_id_set,
)


def _post(pid, uid, text, is_comment=False):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text, is_comment=is_comment)


def test_strip_html():
    assert _strip_html("<p>не <b>печатает</b></p>") == "не печатает"


def test_staff_id_set_prefers_configured():
    assert staff_id_set("owner1", ("10", "20")) == {"10", "20"}
    assert staff_id_set("owner1", ()) == {"owner1"}


def test_split_multi_turn_no_future_leak():
    staff = {"op"}
    posts = [
        _post(1, "client", "касса не печатает"),
        _post(2, "op", "проверьте бумагу"),
        _post(3, "client", "бумага есть, всё равно молчит"),
        _post(4, "op", "перезагрузите кассу"),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 2
    # первая пара: контекст только до первого ответа оператора
    assert pairs[0]["operator_answer"] == "проверьте бумагу"
    assert "касса не печатает" in pairs[0]["context"]
    assert "перезагрузите" not in pairs[0]["context"]        # нет утечки будущего
    assert "перезагрузите" not in pairs[0]["operator_answer"]
    # вторая пара: контекст включает оба клиентских сообщения + первый ответ оператора
    assert pairs[1]["operator_answer"] == "перезагрузите кассу"
    assert "бумага есть" in pairs[1]["context"]
    assert pairs[0]["content_hash"] != pairs[1]["content_hash"]


def test_split_skips_comments_and_leading_staff():
    staff = {"op"}
    posts = [
        _post(1, "op", "внутренняя заметка", is_comment=True),  # комментарий — вон
        _post(2, "op", "ответ без клиента"),                    # нет клиента до — skip
        _post(3, "client", "вопрос"),
        _post(4, "op", "ответ"),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 1
    assert pairs[0]["operator_answer"] == "ответ"
    assert "вопрос" in pairs[0]["context"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_mining.py -k "strip or staff_id or split" -v`
Expected: FAIL — `No module named 'bot.agent.dialogue_mining'`.

- [ ] **Step 3: Implement**

```python
# bot/agent/dialogue_mining.py
"""Нарезка закрытых тикетов HDE на пары «контекст → ответ оператора» (Phase 2A).

Каждый ответ оператора порождает пару с контекстом СТРОГО до этого ответа —
без утечки будущего решения. Все пары multi-turn: N ответов оператора в тикете
дают N пар с нарастающим контекстом.
"""
from __future__ import annotations

import hashlib
import html as _html
import re as _re


def _strip_html(text: str) -> str:
    cleaned = _re.sub(r"<[^>]+>", " ", text or "")
    cleaned = _html.unescape(cleaned)
    return _re.sub(r"\s+", " ", cleaned).strip()


def staff_id_set(owner_id: str, staff_ids: tuple[str, ...]) -> set[str]:
    """Явный список staff-id (если задан) либо владелец как единственный staff."""
    if staff_ids:
        return {str(s) for s in staff_ids}
    return {str(owner_id)}


def _pair_content_hash(ticket_id: str, operator_message_id: str, context: str) -> str:
    raw = f"{ticket_id}|{operator_message_id}|{context}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_ticket_into_pairs(
    ticket_id: str, posts: list, staff: set[str]
) -> list[dict]:
    """Возвращает пары для каждого ответа оператора с непустым клиентским
    контекстом до него. Контекст — только посты ДО ответа (posts[:idx])."""
    ordered = [p for p in posts if not getattr(p, "is_comment", False)]
    pairs: list[dict] = []
    for idx, post in enumerate(ordered):
        if str(post.user_id) not in staff:
            continue
        prior = ordered[:idx]
        client_lines = [
            _strip_html(p.text) for p in prior if str(p.user_id) not in staff
        ]
        client_lines = [t for t in client_lines if t]
        if not client_lines:
            continue  # нет клиентского контекста до ответа оператора
        operator_answer = _strip_html(post.text)
        if not operator_answer:
            continue
        # весь контекст до ответа (клиент + предыдущие ответы оператора)
        context_lines = []
        for p in prior:
            text = _strip_html(p.text)
            if not text:
                continue
            role = "Оператор" if str(p.user_id) in staff else "Клиент"
            context_lines.append(f"{role}: {text}")
        context = "\n".join(context_lines)
        content_hash = _pair_content_hash(ticket_id, str(post.post_id), context)
        pairs.append({
            "ticket_id": ticket_id,
            "source_message_id": str(prior[-1].post_id),
            "context_until_message_id": str(prior[-1].post_id),
            "operator_message_id": str(post.post_id),
            "context": context,
            "operator_answer": operator_answer,
            "content_hash": content_hash,
        })
    return pairs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dialogue_mining.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/dialogue_mining.py tests/test_dialogue_mining.py
git commit -m "feat(dialogue): multi-turn ticket slicing without future-leak"
```

---

## Task 5: mine_ticket_pairs — embed + save one ticket

**Files:**
- Modify: `bot/agent/dialogue_mining.py`
- Test: `tests/test_dialogue_mining.py` (append)

**Interfaces:**
- Consumes: injected HDE client with `get_ticket_posts(ticket_id) -> list`, `get_ticket_info(ticket_id) -> HDETicketInfo` (attr `.client_id`); `bot.knowledge.indexer.embed_text` (default embedder, async → np.ndarray|None); `bot.db.save_dialogue_pair`.
- Produces: `mine_ticket_pairs(client, ticket, staff, *, known_hashes=None, _embed_fn=None, _save_fn=None) -> int` — returns number of NEW pairs saved for one ticket dict (`ticket["id"]`, `ticket.get("type_id")`). Skips hashes already in `known_hashes`. Embedding stored as `np.ndarray.tobytes()`; `None` embedding still saves the pair (embedding NULL).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_mining.py (append)
import numpy as np

from bot.agent.dialogue_mining import mine_ticket_pairs


class _FakeHDE:
    def __init__(self, posts, client_id="client"):
        self._posts = posts
        self._client_id = client_id

    async def get_ticket_posts(self, ticket_id, limit=20):
        return self._posts

    async def get_ticket_info(self, ticket_id):
        from types import SimpleNamespace
        return SimpleNamespace(client_id=self._client_id)


async def test_mine_ticket_pairs_saves_and_embeds():
    posts = [
        _post(1, "client", "вопрос один"),
        _post(2, "op", "ответ один"),
    ]
    saved = {}

    async def fake_embed(text, task_type="passage"):
        return np.ones(4, dtype=np.float32)

    async def fake_save(**kw):
        saved[kw["content_hash"]] = kw
        return len(saved)

    n = await mine_ticket_pairs(
        _FakeHDE(posts), {"id": "T1", "type_id": "5"}, {"op"},
        _embed_fn=fake_embed, _save_fn=fake_save,
    )
    assert n == 1
    (pair,) = saved.values()
    assert pair["ticket_id"] == "T1"
    assert pair["issue_type"] == "5"
    assert pair["client_id"] == "client"
    assert pair["embedding"] is not None
    assert pair["embedding_model"] == "intfloat/multilingual-e5-large"


async def test_mine_ticket_pairs_skips_known_hashes():
    posts = [_post(1, "client", "вопрос"), _post(2, "op", "ответ")]

    async def fake_embed(text, task_type="passage"):
        return None

    calls = []

    async def fake_save(**kw):
        calls.append(kw)
        return 1

    # предвычислить хэш и пометить как известный
    from bot.agent.dialogue_mining import split_ticket_into_pairs
    known = {split_ticket_into_pairs("T1", posts, {"op"})[0]["content_hash"]}
    n = await mine_ticket_pairs(
        _FakeHDE(posts), {"id": "T1"}, {"op"},
        known_hashes=known, _embed_fn=fake_embed, _save_fn=fake_save,
    )
    assert n == 0
    assert calls == []  # ничего не сохранено
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_mining.py -k mine_ticket -v`
Expected: FAIL — `cannot import name 'mine_ticket_pairs'`.

- [ ] **Step 3: Implement**

Append to `bot/agent/dialogue_mining.py`:

```python
_EMBED_MODEL = "intfloat/multilingual-e5-large"


async def mine_ticket_pairs(
    client,
    ticket: dict,
    staff: set[str],
    *,
    known_hashes: set[str] | None = None,
    _embed_fn=None,
    _save_fn=None,
) -> int:
    """Нарезает один тикет на пары, эмбеддит и сохраняет новые. Возвращает
    число сохранённых пар (уже известные по content_hash пропускаются)."""
    if _embed_fn is None:
        from ..knowledge.indexer import embed_text
        _embed_fn = embed_text
    if _save_fn is None:
        from ..db import save_dialogue_pair
        _save_fn = save_dialogue_pair
    known = known_hashes or set()

    ticket_id = str(ticket.get("id") or ticket.get("ticket_id") or "")
    if not ticket_id:
        return 0
    posts = await client.get_ticket_posts(ticket_id)
    pairs = split_ticket_into_pairs(ticket_id, posts, staff)
    if not pairs:
        return 0
    issue_type = str(ticket.get("type_id") or "") or None
    try:
        info = await client.get_ticket_info(ticket_id)
        client_id = str(getattr(info, "client_id", "") or "") or None
    except Exception:
        client_id = None

    saved = 0
    for pair in pairs:
        if pair["content_hash"] in known:
            continue
        embedding = await _embed_fn(pair["operator_answer"], task_type="passage")
        emb_bytes = embedding.tobytes() if embedding is not None else None
        await _save_fn(
            ticket_id=ticket_id,
            context=pair["context"],
            operator_answer=pair["operator_answer"],
            content_hash=pair["content_hash"],
            source_message_id=pair["source_message_id"],
            context_until_message_id=pair["context_until_message_id"],
            operator_message_id=pair["operator_message_id"],
            issue_type=issue_type,
            client_id=client_id,
            resolution_status="closed",
            embedding=emb_bytes,
            embedding_model=_EMBED_MODEL if emb_bytes is not None else None,
        )
        known.add(pair["content_hash"])
        saved += 1
    return saved
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dialogue_mining.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/dialogue_mining.py tests/test_dialogue_mining.py
git commit -m "feat(dialogue): mine_ticket_pairs — embed + save one ticket"
```

---

## Task 6: Resumable CLI backfill

**Files:**
- Create: `scripts/backfill_dialogue_pairs.py`

**Interfaces:**
- Consumes: `bot.hde_api.HDEApiClient`, `bot.config.config` (`hde_owner_id`, `agent_staff_user_ids`), `bot.agent.dialogue_mining` (`staff_id_set`, `mine_ticket_pairs`), `bot.db` (`init_db`, `dialogue_pair_hashes`, `count_dialogue_pairs`, `get_backfill_checkpoint`, `set_backfill_checkpoint`).
- Produces (behavior): mines `--pages` pages starting from `checkpoint+1`, saves checkpoint after each completed page, prints progress; `--status` prints checkpoint + pair count without mining.
- No unit tests — thin CLI (repo convention); smoke-run verifies wiring.

- [ ] **Step 1: Write the CLI**

```python
# scripts/backfill_dialogue_pairs.py
"""Resumable backfill of dialogue_pairs from closed HDE tickets (Phase 2A).

  python scripts/backfill_dialogue_pairs.py --pages 20   # mine next 20 pages
  python scripts/backfill_dialogue_pairs.py --status     # show progress only

Checkpoint (last completed page) persists in bot_settings, so re-runs continue
where they left off. Safe to run across several nights. Rate-limited per ticket.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _status() -> None:
    import bot.db as db
    await db.init_db()
    page = await db.get_backfill_checkpoint()
    total = await db.count_dialogue_pairs()
    print(f"checkpoint page={page}, dialogue_pairs stored={total}")


async def _run(pages: int) -> None:
    import bot.db as db
    from bot.agent.dialogue_mining import mine_ticket_pairs, staff_id_set
    from bot.config import config
    from bot.hde_api import HDEApiClient

    await db.init_db()
    client = HDEApiClient()
    staff = staff_id_set(config.hde_owner_id, config.agent_staff_user_ids)
    known = await db.dialogue_pair_hashes()
    start = await db.get_backfill_checkpoint() + 1
    total_saved = 0
    for offset in range(pages):
        page = start + offset
        tickets, total_pages = await client.get_closed_tickets_page(
            config.hde_owner_id, page
        )
        if not tickets:
            print(f"page {page}: пусто — бэкфилл завершён")
            break
        page_saved = 0
        for ticket in tickets:
            page_saved += await mine_ticket_pairs(
                client, ticket, staff, known_hashes=known
            )
            await asyncio.sleep(0.5)  # rate-limit, как в /aiimport
        await db.set_backfill_checkpoint(page)
        total_saved += page_saved
        print(f"page {page}/{total_pages}: +{page_saved} pairs "
              f"(итого сессии {total_saved})")
        if page >= total_pages:
            print("достигнута последняя страница — бэкфилл завершён")
            break
    print(f"Готово. Сохранено за сессию: {total_saved}. "
          f"Всего пар: {await db.count_dialogue_pairs()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=20,
                        help="сколько страниц обработать за запуск")
    parser.add_argument("--status", action="store_true",
                        help="показать прогресс без майнинга")
    args = parser.parse_args()
    asyncio.run(_status() if args.status else _run(args.pages))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-check parsing**

Run: `python scripts/backfill_dialogue_pairs.py --help`
Expected: usage with `--pages` and `--status`, exit 0.

- [ ] **Step 3: Full suite**

Run: `python -m pytest -q`
Expected: PASS (all, including new dialogue tests).

- [ ] **Step 4: Commit**

```bash
git add scripts/backfill_dialogue_pairs.py
git commit -m "feat(dialogue): resumable CLI backfill with checkpoint"
```

---

## Task 7: Gated nightly increment job

**Files:**
- Modify: `bot/scheduler.py`
- Test: `tests/test_dialogue_mining.py` (append)

**Interfaces:**
- Consumes: `config.agent_enabled`, `bot.agent.dialogue_mining`, `bot.db`.
- Produces: `_maybe_backfill_dialogue_pairs(bot) -> None` — gated on `config.agent_enabled`; runs once per day (in-memory date flag like `_maybe_send_digest`); mines page 1 of closed tickets (newly-closed land on page 1) to pick up recent pairs; wired into `process_scheduled_actions`. When `agent_enabled` is False it returns immediately (default — no behavior change).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_mining.py (append)
import bot.scheduler as scheduler_module


async def test_nightly_increment_skips_when_agent_disabled(monkeypatch):
    cfg = __import__("bot.config", fromlist=["config"]).config
    monkeypatch.setattr(cfg, "agent_enabled", False)
    called = {"mine": False}

    async def fake_mine(*a, **k):
        called["mine"] = True
        return 0

    monkeypatch.setattr(
        "bot.agent.dialogue_mining.mine_ticket_pairs", fake_mine
    )
    # reset the daily flag so the guard isn't the reason it skips
    scheduler_module._last_dialogue_backfill_date = None
    await scheduler_module._maybe_backfill_dialogue_pairs(bot=None)
    assert called["mine"] is False  # gated off → never mines
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_mining.py -k nightly_increment -v`
Expected: FAIL — `module 'bot.scheduler' has no attribute '_maybe_backfill_dialogue_pairs'`.

- [ ] **Step 3: Implement**

In `bot/scheduler.py`, add a module-level flag near the other `_last_*_date` globals:

```python
_last_dialogue_backfill_date: str | None = None
```

Add the job function (mirror `_maybe_send_digest`'s daily-flag structure; run in the night window, e.g. hour 1 MSK via the existing `_now_msk`/date-key pattern):

```python
async def _maybe_backfill_dialogue_pairs(bot) -> None:
    """Ночной инкремент dialogue_pairs (Phase 2A). Гейт: config.agent_enabled.
    Раз в сутки берёт page 1 закрытых тикетов (свежезакрытые тут) и добирает
    новые пары. Полный бэкфилл — отдельным скриптом backfill_dialogue_pairs.py."""
    global _last_dialogue_backfill_date
    from .config import config
    if not config.agent_enabled:
        return
    now = _now_msk()
    today = now.strftime("%Y-%m-%d")
    if _last_dialogue_backfill_date == today:
        return
    if now.hour != 1:
        return
    _last_dialogue_backfill_date = today
    try:
        import bot.db as db
        from .agent.dialogue_mining import mine_ticket_pairs, staff_id_set
        from .hde_api import HDEApiClient
        client = HDEApiClient()
        staff = staff_id_set(config.hde_owner_id, config.agent_staff_user_ids)
        known = await db.dialogue_pair_hashes()
        tickets, _ = await client.get_closed_tickets_page(config.hde_owner_id, 1)
        saved = 0
        for ticket in tickets:
            saved += await mine_ticket_pairs(client, ticket, staff, known_hashes=known)
        logger.info("Nightly dialogue backfill: +%d pairs", saved)
    except Exception as exc:
        logger.warning("Nightly dialogue backfill failed: %s", exc)
```

Wire it into `process_scheduled_actions` alongside the other `await _maybe_*` calls:

```python
        await _maybe_backfill_dialogue_pairs(bot)
```

Match the exact call style of the neighboring `_maybe_*` invocations in that function (check whether they're awaited or wrapped). If `process_scheduled_actions` is not `async`/does not `await`, follow the file's actual pattern for scheduling an async job there (e.g. `asyncio.create_task`), matching how `_maybe_send_digest` is invoked.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dialogue_mining.py -k nightly_increment -v`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

Run: `python -m pytest -q`
Expected: PASS.

```bash
git add bot/scheduler.py tests/test_dialogue_mining.py
git commit -m "feat(dialogue): gated nightly increment job (off by default)"
```

---

## Task 8: Full-suite verification

- [ ] **Step 1:** Run `python -m pytest -q` — expected PASS, no regressions.
- [ ] **Step 2:** Run `python -c "import bot.main" 2>&1 | tail -1` — expected clean import (scheduler wiring valid).
- [ ] **Step 3:** Commit any fixups: `git commit -m "test(dialogue): phase 2A verification" --allow-empty`.

---

## Self-Review

**Spec coverage (Phase 2A):**
- `dialogue_pairs` table with spec columns → Task 1. ✓
- Multi-turn slicing, context strictly before operator answer (no future-leak) → Task 4. ✓
- Staff detection (configurable ids, owner fallback, comments excluded) → Tasks 3–4. ✓
- e5-large embedding per pair, model name stored → Task 5. ✓
- Backfill all closed tickets (~1000–5000), resumable, rate-limited → Task 6 (checkpoint). ✓
- Incremental newly-closed pickup → Task 7 (nightly, gated off). ✓
- `quality_status='unreviewed'` default; content_hash dedup → Tasks 1–2. ✓
- No runtime coupling until enabled → nightly job gated on `agent_enabled`; CLI is offline. ✓

**Placeholder scan:** clean. The one soft spot — Task 7 Step 3 says "match the file's actual invocation pattern" for wiring into `process_scheduled_actions`; this is deliberate because the exact await/create_task shape must match the neighboring `_maybe_*` calls verbatim, which the implementer reads in-file.

**Type consistency:** `save_dialogue_pair` kwargs (Task 2) match `mine_ticket_pairs`'s `_save_fn` call (Task 5) and `dialogue_store` exports; `split_ticket_into_pairs` dict keys (Task 4) are exactly what `mine_ticket_pairs` reads; `staff_id_set` signature shared by Tasks 4/6/7; `_EMBED_MODEL` string matches the spec's e5-large name and Phase 2A embedding_model constraint.
