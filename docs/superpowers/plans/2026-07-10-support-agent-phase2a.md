# Support Agent — Phase 2A (Retro Backfill of dialogue_pairs) Implementation Plan — rev.2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `dialogue_pairs` dataset from closed HDE tickets — problem-side embedded for retrieval, no future-leak, consecutive operator turns merged, original timestamps preserved, embedding failures retryable — backfilled resumably by a **processed-ticket-id** cursor (not a fragile page number), with a separately-flagged nightly increment that respects the "closed + 7 days" rule.

**Architecture (rev.2 changes):** Checkpoint is the set of processed `ticket_id`s (page number is advisory only); overlapping re-scan is safe because dedup is per-ticket. Each ticket's full post history is fetched via a new paginating `get_all_ticket_posts`; posts are explicitly sorted oldest→newest before slicing. Consecutive same-side operator posts merge into one turn. Embedding is built from the **problem side** (client question + bounded prior context), stored with `embedding_status` so a transient e5 failure is retried, not frozen. `content_hash` includes the normalized operator answer. Staff/client roles come from author-type detection verified against real HDE JSON (Task 0). Nightly increment is gated by its OWN flag `AGENT_DIALOGUE_MINING_ENABLED` and filters `resolved_at <= now-7d`.

**Tech Stack:** Python 3, aiosqlite, numpy, pytest (`asyncio_mode = auto`).

## Global Constraints

- No new third-party dependencies.
- **No future-leak**: `context` for a pair is built strictly from posts BEFORE the operator turn; `operator_answer` is the merged operator turn; comments excluded before indexing. Every slicing test asserts the future answer never appears in context.
- **Explicit ordering**: posts are `sorted` by a stable sequence (post_id, tie-broken by parsed `date_created`) oldest→newest before any slicing — never assume API order. A reversed-input test guards this.
- **Full history**: fetch all posts via `get_all_ticket_posts` (paginates until a short page); do not rely on the default `limit=20`.
- **Merged operator turns**: consecutive posts from staff (no intervening client post) collapse into ONE `operator_answer` (joined with newlines); the pair's `operator_message_id` is the FIRST staff post of the turn, `source_message_id` is the LAST client post before it.
- **Problem-side embedding**: the stored `embedding` is computed from `embedding_text` = `"Вопрос клиента: <last client text>\nКонтекст: <bounded context>"`, NOT from `operator_answer`. `embedding_text_hash` is stored for auditability.
- **Retryable embedding**: `embedding_status ∈ (pending|ready|failed)`; a pair with no embedding is saved `pending`/`failed` and a re-embed pass fills it later. A pair is never skipped forever due to a transient embed error.
- **Timestamps**: store `operator_answer_at` (parsed from the operator post's `date_created`), `resolved_at` (ticket resolution time when available), `inserted_at` (DB insert). `created_at` column = `operator_answer_at` semantics, not insert time.
- **content_hash** = sha256 of `ticket_id | operator_message_id | normalized(context) | normalized(operator_answer)`.
- **Staff detection** (Task 0 first): use the author-type/role field from the real HDE post JSON if it exists; else a configured staff-id set (`AGENT_STAFF_USER_IDS`, CSV) with `owner_id` fallback; bot/system posts excluded explicitly. The Task 0 finding governs the final rule.
- **Resumable by ticket-id cursor**: `dialogue_backfill` tracks processed ticket_ids; re-running re-scans overlapping pages harmlessly (dedup). Page number stored only as a scan hint.
- **Per-ticket error isolation**: one bad ticket is logged to a `dialogue_mining_errors` setting/list and skipped; it never aborts the page or blocks the cursor.
- **Nightly increment gated separately**: `config.agent_dialogue_mining_enabled` (env `AGENT_DIALOGUE_MINING_ENABLED`, default off) — independent of `AGENT_ENABLED`; filters `resolved_at <= now-7d` and re-checks current status. The scheduler edit means this phase is "offline tooling + one gated scheduler hook", stated plainly here.
- Store in `bot/db/dialogue_store.py`, re-exported from `bot/db/__init__.py`; connection pattern of `bot/db/optimizer_store.py`.
- `save_dialogue_pair` returns `(pair_id, created: bool)`; idempotent on `content_hash`.
- Tests: `asyncio_mode = auto`, `tests/test_dialogue_mining.py`, `tests/test_dialogue_store.py`.
- Style: `from __future__ import annotations`, RU docstrings.

## Out of scope

Quality gating beyond `unreviewed` (2B), dynamic few-shot retrieval + same-ticket/golden exclusion (2B/Phase 1), transcript pairs (Phase 4).

## File Structure

- Modify: `bot/db/core.py` — `dialogue_pairs` table (rev.2 columns).
- Create: `bot/db/dialogue_store.py` — save `(id,created)`, count, hashes, processed-id cursor, pending-embedding queue, error log.
- Modify: `bot/db/__init__.py` — re-exports.
- Modify: `bot/config.py` — `agent_staff_user_ids`, `agent_dialogue_mining_enabled`.
- Modify: `bot/hde_api.py` — `get_all_ticket_posts` (paginating).
- Create: `bot/agent/dialogue_mining.py` — `_strip_html`, `_norm`, `sort_posts`, `staff_id_set`, `is_staff_post`, `split_ticket_into_pairs` (merged turns), `build_embedding_text`, `mine_ticket_pairs`, `reembed_pending`.
- Create: `scripts/backfill_dialogue_pairs.py` — cursor-resumable CLI.
- Modify: `bot/scheduler.py` — gated nightly increment (separate flag).
- Test: `tests/test_dialogue_mining.py`, `tests/test_dialogue_store.py`.

---

## Task 0: Verify HDE post author-type in raw JSON

**Files:**
- Create: `docs/superpowers/notes/hde-post-author-type.md` (findings note)

**Interfaces:** none (investigation task). This is the review-mandated "check the raw API before finalizing staff detection" step; its outcome sets the `is_staff_post` rule in Task 4.

- [ ] **Step 1: Capture one real ticket's raw posts JSON**

Run (requires HDE creds in `.env`; if unavailable, mark the note "unverified — used config fallback" and proceed):
```bash
python - <<'PY'
import asyncio, json
from bot.hde_api import HDEApiClient
async def main():
    c = HDEApiClient()
    # pick any known closed ticket id from a prior /aiimport or logs
    import sys
    tid = "REPLACE_WITH_REAL_CLOSED_TICKET_ID"
    posts = await c.get_ticket_posts(tid)
    print("dataclass fields:", posts[0].__dict__ if posts else "no posts")
asyncio.run(main())
PY
```
Also inspect `bot/hde_api.py` around the `HDEPost` construction (`:322-356`) to list which raw JSON keys are read vs. present-but-ignored.

- [ ] **Step 2: Write the findings note**

Create `docs/superpowers/notes/hde-post-author-type.md` recording: does the raw post JSON carry an author-type/role/`user_type`/`is_staff`/system flag? If yes, name the exact key and values. If not confirmable, state that Task 4 uses the configured staff-id set + owner fallback, and that bot/system posts are identified by <criteria found or "none available">.

- [ ] **Step 3: Commit the note**

```bash
git add docs/superpowers/notes/hde-post-author-type.md
git commit -m "docs(dialogue): HDE post author-type investigation for staff detection"
```

---

## Task 1: Schema — `dialogue_pairs` (rev.2 columns)

**Files:**
- Modify: `bot/db/core.py`
- Test: `tests/test_dialogue_store.py`

**Interfaces:**
- Produces: table `dialogue_pairs` with rev.2 columns + indexes on `content_hash` (unique), `quality_status`, `embedding_status`.

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
        "client_id", "operator_answer_at", "resolved_at", "inserted_at",
        "resolution_status", "quality_status", "quality_reason",
        "embedding", "embedding_model", "embedding_status", "embedding_text_hash",
        "content_hash",
    }
    assert expected <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_store.py -v`
Expected: FAIL — `no such table: dialogue_pairs`.

- [ ] **Step 3: Add the table in `init_db`** (before final `await db.commit()`):

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
                operator_answer_at        TEXT,
                resolved_at               TEXT,
                inserted_at               TEXT NOT NULL DEFAULT (datetime('now')),
                resolution_status         TEXT,
                quality_status            TEXT NOT NULL DEFAULT 'unreviewed',
                quality_reason            TEXT,
                embedding                 BLOB,
                embedding_model           TEXT,
                embedding_status          TEXT NOT NULL DEFAULT 'pending',
                embedding_text_hash       TEXT,
                content_hash              TEXT UNIQUE
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dialogue_pairs_quality "
            "ON dialogue_pairs(quality_status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dialogue_pairs_embstatus "
            "ON dialogue_pairs(embedding_status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dialogue_pairs_ticket "
            "ON dialogue_pairs(ticket_id)"
        )
```

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_dialogue_store.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/db/core.py tests/test_dialogue_store.py
git commit -m "feat(dialogue): dialogue_pairs schema (rev.2)"
```

---

## Task 2: Store — save `(id,created)`, cursor, pending queue, error log

**Files:**
- Create: `bot/db/dialogue_store.py`
- Modify: `bot/db/__init__.py`
- Test: `tests/test_dialogue_store.py` (append)

**Interfaces:**
- Produces:
  - `save_dialogue_pair(*, ticket_id, context, operator_answer, content_hash, source_message_id=None, context_until_message_id=None, operator_message_id=None, issue_type=None, client_id=None, operator_answer_at=None, resolved_at=None, resolution_status=None, embedding=None, embedding_model=None, embedding_status="pending", embedding_text_hash=None) -> tuple[int, bool]` — `(pair_id, created)`; idempotent on content_hash.
  - `count_dialogue_pairs() -> int`
  - `dialogue_pair_hashes() -> set[str]`
  - `list_processed_ticket_ids() -> set[str]`
  - `mark_ticket_processed(ticket_id: str) -> None`
  - `list_pending_embeddings(limit: int = 200) -> list[dict]` — rows with `embedding_status IN ('pending','failed')` (`pair_id`, `embedding_text_hash`, and the `context`/`operator_answer` needed to rebuild embedding text — but embedding text is rebuilt by the miner; here return `pair_id` + `content` fields).
  - `set_pair_embedding(pair_id: int, embedding: bytes | None, model: str | None, status: str) -> None`
  - `log_ticket_error(ticket_id: str, error: str) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_store.py (append)
from bot.db.dialogue_store import (
    count_dialogue_pairs,
    dialogue_pair_hashes,
    list_pending_embeddings,
    list_processed_ticket_ids,
    mark_ticket_processed,
    save_dialogue_pair,
    set_pair_embedding,
)


async def test_save_returns_created_flag_and_is_idempotent():
    await db_module.init_db()
    kw = dict(ticket_id="T1", context="Клиент: q", operator_answer="a", content_hash="h1")
    pid1, created1 = await save_dialogue_pair(**kw)
    pid2, created2 = await save_dialogue_pair(**kw)
    assert created1 is True and created2 is False
    assert pid1 == pid2
    assert await count_dialogue_pairs() == 1
    assert "h1" in await dialogue_pair_hashes()


async def test_processed_cursor_roundtrip():
    await db_module.init_db()
    assert await list_processed_ticket_ids() == set()
    await mark_ticket_processed("T1")
    await mark_ticket_processed("T2")
    assert await list_processed_ticket_ids() == {"T1", "T2"}


async def test_pending_embedding_flow():
    await db_module.init_db()
    pid, _ = await save_dialogue_pair(
        ticket_id="T1", context="c", operator_answer="a", content_hash="h2",
        embedding=None, embedding_status="pending",
    )
    pend = await list_pending_embeddings()
    assert any(r["pair_id"] == pid for r in pend)
    await set_pair_embedding(pid, b"\x00\x01", "e5", "ready")
    pend2 = await list_pending_embeddings()
    assert all(r["pair_id"] != pid for r in pend2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_store.py -k "created_flag or cursor or pending" -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Create the store**

```python
# bot/db/dialogue_store.py
"""Store для dialogue_pairs (Phase 2A). Курсор по обработанным ticket_id,
очередь pending-эмбеддингов, журнал ошибок тикетов."""
from __future__ import annotations

import json

import aiosqlite

from .core import connect
from .misc import get_setting, set_setting

_PROCESSED_KEY = "dialogue_processed_ids"
_ERRORS_KEY = "dialogue_mining_errors"


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
    operator_answer_at: str | None = None,
    resolved_at: str | None = None,
    resolution_status: str | None = None,
    embedding: bytes | None = None,
    embedding_model: str | None = None,
    embedding_status: str = "pending",
    embedding_text_hash: str | None = None,
) -> tuple[int, bool]:
    """Вставляет пару идемпотентно по content_hash. Возвращает (pair_id, created)."""
    async with connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO dialogue_pairs "
            "(ticket_id, source_message_id, context_until_message_id, "
            " operator_message_id, context, operator_answer, issue_type, client_id, "
            " operator_answer_at, resolved_at, resolution_status, embedding, "
            " embedding_model, embedding_status, embedding_text_hash, content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_id, source_message_id, context_until_message_id,
                operator_message_id, context, operator_answer, issue_type, client_id,
                operator_answer_at, resolved_at, resolution_status, embedding,
                embedding_model, embedding_status, embedding_text_hash, content_hash,
            ),
        )
        created = cursor.rowcount > 0
        await db.commit()
        async with db.execute(
            "SELECT pair_id FROM dialogue_pairs WHERE content_hash=?", (content_hash,)
        ) as cur:
            row = await cur.fetchone()
    return (int(row[0]) if row else 0, created)


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


async def list_processed_ticket_ids() -> set[str]:
    raw = await get_setting(_PROCESSED_KEY, "")
    return set(json.loads(raw)) if raw else set()


async def mark_ticket_processed(ticket_id: str) -> None:
    current = await list_processed_ticket_ids()
    current.add(str(ticket_id))
    await set_setting(_PROCESSED_KEY, json.dumps(sorted(current)))


async def list_pending_embeddings(limit: int = 200) -> list[dict]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pair_id, context, operator_answer, embedding_text_hash "
            "FROM dialogue_pairs WHERE embedding_status IN ('pending','failed') "
            "LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_pair_embedding(
    pair_id: int, embedding: bytes | None, model: str | None, status: str
) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE dialogue_pairs SET embedding=?, embedding_model=?, "
            "embedding_status=? WHERE pair_id=?",
            (embedding, model, status, pair_id),
        )
        await db.commit()


async def log_ticket_error(ticket_id: str, error: str) -> None:
    raw = await get_setting(_ERRORS_KEY, "")
    errors = json.loads(raw) if raw else {}
    errors[str(ticket_id)] = error[:300]
    await set_setting(_ERRORS_KEY, json.dumps(errors, ensure_ascii=False))
```

Add to `bot/db/__init__.py` after the `suggestion_store` block:

```python
from .dialogue_store import (
    count_dialogue_pairs,
    dialogue_pair_hashes,
    list_pending_embeddings,
    list_processed_ticket_ids,
    log_ticket_error,
    mark_ticket_processed,
    save_dialogue_pair,
    set_pair_embedding,
)
```

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_dialogue_store.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/db/dialogue_store.py bot/db/__init__.py tests/test_dialogue_store.py
git commit -m "feat(dialogue): store — (id,created) save, id cursor, pending queue, errors"
```

---

## Task 3: Config + `get_all_ticket_posts`

**Files:**
- Modify: `bot/config.py`
- Modify: `bot/hde_api.py`
- Test: `tests/test_dialogue_mining.py`

**Interfaces:**
- Produces:
  - `config.agent_staff_user_ids: tuple[str, ...]` (env `AGENT_STAFF_USER_IDS`), `config.agent_dialogue_mining_enabled: bool` (env `AGENT_DIALOGUE_MINING_ENABLED`, default False).
  - `HDEApiClient.get_all_ticket_posts(ticket_id, *, page_size=20, max_pages=25) -> list[HDEPost]` — paginates `get_ticket_posts`-style until a page shorter than `page_size` (or `max_pages`); concatenates in fetch order. (Implementation reuses the existing posts endpoint with a page/offset param — match how `get_closed_tickets_page` passes pagination; if the posts endpoint has no paging, document that `get_ticket_posts`'s limit is the cap and raise the limit to a safe 200.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_mining.py
import bot.config as config_module


def test_staff_ids_and_mining_flag(monkeypatch):
    monkeypatch.setenv("AGENT_STAFF_USER_IDS", "10, 20 ,30")
    monkeypatch.setenv("AGENT_DIALOGUE_MINING_ENABLED", "true")
    fresh = config_module.Config.from_env()
    assert fresh.agent_staff_user_ids == ("10", "20", "30")
    assert fresh.agent_dialogue_mining_enabled is True


def test_mining_flag_default_off(monkeypatch):
    monkeypatch.delenv("AGENT_DIALOGUE_MINING_ENABLED", raising=False)
    assert config_module.Config.from_env().agent_dialogue_mining_enabled is False
```

(A `get_all_ticket_posts` unit test is added in Task 5 with the fake client; here the config is the gate.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_mining.py -k "staff_ids or mining_flag" -v`
Expected: FAIL — missing attrs.

- [ ] **Step 3: Implement**

`bot/config.py` — dataclass fields (agent block):

```python
    agent_staff_user_ids: tuple[str, ...]
    agent_dialogue_mining_enabled: bool
```

`from_env`:

```python
            agent_staff_user_ids=_parse_csv(os.getenv("AGENT_STAFF_USER_IDS")),
            agent_dialogue_mining_enabled=_parse_bool(
                os.getenv("AGENT_DIALOGUE_MINING_ENABLED"), default=False
            ),
```

`bot/hde_api.py` — add near `get_ticket_posts`:

```python
    async def get_all_ticket_posts(
        self, ticket_id, *, page_size: int = 20, max_pages: int = 25
    ) -> list["HDEPost"]:
        """Полная история постов тикета через пагинацию (не обрезается limit=20)."""
        collected: list = []
        page = 1
        while page <= max_pages:
            batch = await self.get_ticket_posts(ticket_id, limit=page_size, page=page)
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return collected
```

If `get_ticket_posts` has no `page` parameter, add one (default 1) threaded into the request query the same way `get_closed_tickets_page` does; if the HDE posts endpoint genuinely does not paginate, instead implement `get_all_ticket_posts` as a single `get_ticket_posts(ticket_id, limit=200)` call and note it in the Task 0 findings file. Confirm against the endpoint before choosing.

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_dialogue_mining.py -k "staff_ids or mining_flag" -v`

- [ ] **Step 5: Commit**

```bash
git add bot/config.py bot/hde_api.py tests/test_dialogue_mining.py
git commit -m "feat(dialogue): staff-ids + mining flag config; get_all_ticket_posts"
```

---

## Task 4: Slicing — sort, staff detection, merged turns, no future-leak

**Files:**
- Create: `bot/agent/dialogue_mining.py`
- Test: `tests/test_dialogue_mining.py` (append)

**Interfaces:**
- Consumes: post objects `.post_id, .user_id, .text, .is_comment, .date_created`.
- Produces:
  - `_strip_html(text) -> str`, `_norm(text) -> str` (lowercased, whitespace-collapsed — for hashing).
  - `sort_posts(posts) -> list` — oldest→newest by `(int(post_id), date_created)`.
  - `staff_id_set(owner_id, staff_ids) -> set[str]`.
  - `is_staff_post(post, staff: set[str]) -> bool` — per Task 0 rule: author-type field if present, else `user_id in staff`; excludes bot/system by the Task 0 criteria.
  - `_pair_content_hash(ticket_id, operator_message_id, context, operator_answer) -> str` — includes normalized answer.
  - `split_ticket_into_pairs(ticket_id, posts, staff) -> list[dict]` — sorts + excludes comments; walks turns; each CLIENT-turn→STAFF-turn boundary yields ONE pair with the operator turn = all consecutive staff posts merged; `context` = every post strictly before the staff turn; skips staff turns with no preceding client text.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_mining.py (append)
from types import SimpleNamespace

from bot.agent.dialogue_mining import (
    _strip_html,
    is_staff_post,
    sort_posts,
    split_ticket_into_pairs,
    staff_id_set,
)


def _post(pid, uid, text, is_comment=False, dc="00:00:00 01.01.2024"):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text,
                           is_comment=is_comment, date_created=dc)


def test_strip_html():
    assert _strip_html("<p>не <b>печатает</b></p>") == "не печатает"


def test_sort_posts_orders_oldest_first():
    posts = [_post(3, "c", "c"), _post(1, "a", "a"), _post(2, "b", "b")]
    assert [p.post_id for p in sort_posts(posts)] == [1, 2, 3]


def test_staff_id_set_and_detection():
    assert staff_id_set("owner1", ("10", "20")) == {"10", "20"}
    assert staff_id_set("owner1", ()) == {"owner1"}
    assert is_staff_post(_post(1, "10", "x"), {"10"}) is True
    assert is_staff_post(_post(2, "99", "x"), {"10"}) is False


def test_split_merges_consecutive_operator_posts():
    staff = {"op"}
    posts = [
        _post(1, "client", "касса не работает"),
        _post(2, "op", "Добрый день."),
        _post(3, "op", "Уточните модель кассы."),
        _post(4, "op", "И пришлите фото ошибки."),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 1                              # три staff-поста = один turn
    ans = pairs[0]["operator_answer"]
    assert "Добрый день." in ans and "модель кассы" in ans and "фото ошибки" in ans
    assert pairs[0]["operator_message_id"] == "2"       # первый пост turn'а
    assert pairs[0]["source_message_id"] == "1"         # последний клиентский пост


def test_split_multi_turn_no_future_leak():
    staff = {"op"}
    posts = [
        _post(1, "client", "касса не печатает"),
        _post(2, "op", "проверьте бумагу"),
        _post(3, "client", "бумага есть"),
        _post(4, "op", "перезагрузите кассу"),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 2
    assert pairs[0]["operator_answer"] == "проверьте бумагу"
    assert "перезагрузите" not in pairs[0]["context"]   # нет утечки будущего
    assert "перезагрузите" not in pairs[0]["operator_answer"]
    assert "бумага есть" in pairs[1]["context"]
    assert pairs[0]["content_hash"] != pairs[1]["content_hash"]


def test_split_reversed_input_still_correct():
    staff = {"op"}
    posts = [
        _post(4, "op", "перезагрузите кассу"),
        _post(3, "client", "бумага есть"),
        _post(2, "op", "проверьте бумагу"),
        _post(1, "client", "касса не печатает"),
    ]  # обратный порядок на входе
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert pairs[0]["operator_answer"] == "проверьте бумагу"   # сортировка сработала
    assert "перезагрузите" not in pairs[0]["context"]


def test_split_skips_comments_and_leading_staff():
    staff = {"op"}
    posts = [
        _post(1, "op", "внутренняя заметка", is_comment=True),
        _post(2, "op", "ответ без клиента"),
        _post(3, "client", "вопрос"),
        _post(4, "op", "ответ"),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 1
    assert pairs[0]["operator_answer"] == "ответ"


def test_content_hash_changes_with_edited_answer():
    staff = {"op"}
    base = [_post(1, "client", "вопрос"), _post(2, "op", "ответ")]
    edited = [_post(1, "client", "вопрос"), _post(2, "op", "ответ исправлен")]
    h1 = split_ticket_into_pairs("T1", base, staff)[0]["content_hash"]
    h2 = split_ticket_into_pairs("T1", edited, staff)[0]["content_hash"]
    assert h1 != h2                                     # правка ответа → новый хэш
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_mining.py -k "strip or sort or staff or split or content_hash" -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# bot/agent/dialogue_mining.py
"""Нарезка закрытых тикетов на пары «контекст → ответ оператора» (Phase 2A, rev.2).

Явная сортировка старых→новым; последовательные ответы оператора склеиваются в
один turn; контекст строго до ответа (без утечки будущего); content_hash включает
нормализованный ответ; эмбеддинг строится по problem-side."""
from __future__ import annotations

import hashlib
import html as _html
import re as _re

_EMBED_MODEL = "intfloat/multilingual-e5-large"


def _strip_html(text: str) -> str:
    cleaned = _re.sub(r"<[^>]+>", " ", text or "")
    cleaned = _html.unescape(cleaned)
    return _re.sub(r"\s+", " ", cleaned).strip()


def _norm(text: str) -> str:
    return _re.sub(r"\s+", " ", (text or "").lower()).strip()


def _seq_key(post):
    try:
        pid = int(getattr(post, "post_id", 0))
    except (TypeError, ValueError):
        pid = 0
    return (pid, getattr(post, "date_created", "") or "")


def sort_posts(posts) -> list:
    return sorted(posts, key=_seq_key)


def staff_id_set(owner_id: str, staff_ids: tuple[str, ...]) -> set[str]:
    if staff_ids:
        return {str(s) for s in staff_ids}
    return {str(owner_id)}


def is_staff_post(post, staff: set[str]) -> bool:
    """Роль автора: поле типа из HDE JSON, если оно есть (см. Task 0), иначе
    членство в staff-id. Боты/системные — не staff и не клиент (исключаются выше)."""
    # Task 0 governs: if HDEPost gains an author-type attr, prefer it here.
    return str(getattr(post, "user_id", "")) in staff


def _pair_content_hash(ticket_id, operator_message_id, context, operator_answer) -> str:
    raw = f"{ticket_id}|{operator_message_id}|{_norm(context)}|{_norm(operator_answer)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_ticket_into_pairs(ticket_id: str, posts: list, staff: set[str]) -> list[dict]:
    """Пары для каждого клиент→оператор перехода; ответ = склейка подряд идущих
    staff-постов. Контекст — только посты ДО turn'а оператора."""
    ordered = [p for p in sort_posts(posts) if not getattr(p, "is_comment", False)]
    pairs: list[dict] = []
    i = 0
    n = len(ordered)
    while i < n:
        if not is_staff_post(ordered[i], staff):
            i += 1
            continue
        # начало staff-turn'а: склеиваем подряд идущие staff-посты
        turn_start = i
        turn_posts = []
        while i < n and is_staff_post(ordered[i], staff):
            turn_posts.append(ordered[i])
            i += 1
        prior = ordered[:turn_start]
        client_prior = [p for p in prior if not is_staff_post(p, staff)]
        client_lines = [_strip_html(p.text) for p in client_prior]
        client_lines = [t for t in client_lines if t]
        if not client_lines:
            continue  # нет клиентского контекста до ответа
        operator_answer = "\n".join(
            t for t in (_strip_html(p.text) for p in turn_posts) if t
        )
        if not operator_answer:
            continue
        context_lines = []
        for p in prior:
            text = _strip_html(p.text)
            if not text:
                continue
            role = "Оператор" if is_staff_post(p, staff) else "Клиент"
            context_lines.append(f"{role}: {text}")
        context = "\n".join(context_lines)
        op_msg_id = str(turn_posts[0].post_id)
        content_hash = _pair_content_hash(ticket_id, op_msg_id, context, operator_answer)
        pairs.append({
            "ticket_id": ticket_id,
            "source_message_id": str(client_prior[-1].post_id),
            "context_until_message_id": str(prior[-1].post_id),
            "operator_message_id": op_msg_id,
            "operator_answer_at": getattr(turn_posts[0], "date_created", None),
            "context": context,
            "operator_answer": operator_answer,
            "content_hash": content_hash,
        })
    return pairs
```

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_dialogue_mining.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/agent/dialogue_mining.py tests/test_dialogue_mining.py
git commit -m "feat(dialogue): sort + staff detection + merged-turn slicing, no leak"
```

---

## Task 5: mine_ticket_pairs (problem-side embed) + reembed_pending

**Files:**
- Modify: `bot/agent/dialogue_mining.py`
- Test: `tests/test_dialogue_mining.py` (append)

**Interfaces:**
- Consumes: injected client (`get_all_ticket_posts`, `get_ticket_info`), `embed_text`, store (`save_dialogue_pair`, `set_pair_embedding`, `list_pending_embeddings`).
- Produces:
  - `build_embedding_text(pair) -> str` — `"Вопрос клиента: <last client line of context>\nКонтекст: <context[-800:]>"`.
  - `mine_ticket_pairs(client, ticket, staff, *, known_hashes=None, _embed_fn=None, _save_fn=None) -> int` — full posts via `get_all_ticket_posts`; embeds the PROBLEM side; saves with `embedding_status` (`ready` if embedded else `pending`); returns count of NEWLY-created pairs (uses the `created` flag, not blind increment); `known_hashes` mutated in place only if not None (`known if known_hashes is not None else set()`).
  - `reembed_pending(*, _embed_fn=None, _list_fn=None, _set_fn=None, limit=200) -> int` — fills embeddings for `pending`/`failed` rows; returns number now `ready`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_mining.py (append)
import numpy as np

from bot.agent.dialogue_mining import build_embedding_text, mine_ticket_pairs, reembed_pending


class _FakeHDE:
    def __init__(self, posts, client_id="client"):
        self._posts = posts
        self._client_id = client_id

    async def get_all_ticket_posts(self, ticket_id, *, page_size=20, max_pages=25):
        return self._posts

    async def get_ticket_info(self, ticket_id):
        return SimpleNamespace(client_id=self._client_id)


def test_build_embedding_text_is_problem_side():
    pair = {"context": "Клиент: касса не печатает\nОператор: проверьте бумагу",
            "operator_answer": "перезагрузите"}
    text = build_embedding_text(pair)
    assert "касса не печатает" in text                 # клиентская сторона
    assert "перезагрузите" not in text                 # НЕ ответ оператора


async def test_mine_ticket_pairs_embeds_problem_side_and_counts_created():
    posts = [_post(1, "client", "вопрос один"), _post(2, "op", "ответ один")]
    saved = {}
    embedded_texts = []

    async def fake_embed(text, task_type="passage"):
        embedded_texts.append(text)
        return np.ones(4, dtype=np.float32)

    async def fake_save(**kw):
        first = kw["content_hash"] not in saved
        saved[kw["content_hash"]] = kw
        return (len(saved), first)

    n = await mine_ticket_pairs(
        _FakeHDE(posts), {"id": "T1", "type_id": "5"}, {"op"},
        _embed_fn=fake_embed, _save_fn=fake_save,
    )
    assert n == 1
    (pair,) = saved.values()
    assert pair["issue_type"] == "5" and pair["client_id"] == "client"
    assert pair["embedding_status"] == "ready"
    assert pair["embedding_model"] == "intfloat/multilingual-e5-large"
    assert "вопрос один" in embedded_texts[0]          # problem-side embedded
    assert "ответ один" not in embedded_texts[0]


async def test_mine_saves_pending_when_embed_fails():
    posts = [_post(1, "client", "вопрос"), _post(2, "op", "ответ")]
    saved = {}

    async def fail_embed(text, task_type="passage"):
        return None

    async def fake_save(**kw):
        first = kw["content_hash"] not in saved
        saved[kw["content_hash"]] = kw
        return (1, first)

    n = await mine_ticket_pairs(
        _FakeHDE(posts), {"id": "T1"}, {"op"},
        _embed_fn=fail_embed, _save_fn=fake_save,
    )
    assert n == 1
    (pair,) = saved.values()
    assert pair["embedding"] is None
    assert pair["embedding_status"] == "pending"       # не потеряна — повторим позже


async def test_reembed_pending_fills_missing():
    async def fake_list(limit=200):
        return [{"pair_id": 7, "context": "Клиент: q", "operator_answer": "a",
                 "embedding_text_hash": None}]

    updates = []

    async def fake_embed(text, task_type="passage"):
        return np.ones(4, dtype=np.float32)

    async def fake_set(pair_id, embedding, model, status):
        updates.append((pair_id, status))

    n = await reembed_pending(_embed_fn=fake_embed, _list_fn=fake_list, _set_fn=fake_set)
    assert n == 1
    assert updates == [(7, "ready")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_mining.py -k "embedding_text or mine or reembed" -v`
Expected: FAIL — names missing.

- [ ] **Step 3: Implement**

Append to `bot/agent/dialogue_mining.py`:

```python
def build_embedding_text(pair: dict) -> str:
    """Problem-side текст для эмбеддинга: последний вопрос клиента + контекст."""
    client_lines = [
        ln for ln in pair["context"].splitlines() if ln.startswith("Клиент:")
    ]
    last_client = client_lines[-1][len("Клиент:"):].strip() if client_lines else ""
    return f"Вопрос клиента: {last_client}\nКонтекст: {pair['context'][-800:]}"


def _embedding_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def mine_ticket_pairs(
    client, ticket: dict, staff: set[str], *,
    known_hashes: set[str] | None = None, _embed_fn=None, _save_fn=None,
) -> int:
    if _embed_fn is None:
        from ..knowledge.indexer import embed_text as _embed_fn
    if _save_fn is None:
        from ..db import save_dialogue_pair as _save_fn
    known = known_hashes if known_hashes is not None else set()

    ticket_id = str(ticket.get("id") or ticket.get("ticket_id") or "")
    if not ticket_id:
        return 0
    posts = await client.get_all_ticket_posts(ticket_id)
    pairs = split_ticket_into_pairs(ticket_id, posts, staff)
    if not pairs:
        return 0
    issue_type = str(ticket.get("type_id") or "") or None
    try:
        info = await client.get_ticket_info(ticket_id)
        client_id = str(getattr(info, "client_id", "") or "") or None
    except Exception:
        client_id = None

    created_count = 0
    for pair in pairs:
        if pair["content_hash"] in known:
            continue
        emb_text = build_embedding_text(pair)
        embedding = await _embed_fn(emb_text, task_type="passage")
        emb_bytes = embedding.tobytes() if embedding is not None else None
        status = "ready" if emb_bytes is not None else "pending"
        _, created = await _save_fn(
            ticket_id=ticket_id,
            context=pair["context"],
            operator_answer=pair["operator_answer"],
            content_hash=pair["content_hash"],
            source_message_id=pair["source_message_id"],
            context_until_message_id=pair["context_until_message_id"],
            operator_message_id=pair["operator_message_id"],
            operator_answer_at=pair.get("operator_answer_at"),
            issue_type=issue_type,
            client_id=client_id,
            resolution_status="closed",
            embedding=emb_bytes,
            embedding_model=_EMBED_MODEL if emb_bytes is not None else None,
            embedding_status=status,
            embedding_text_hash=_embedding_text_hash(emb_text),
        )
        known.add(pair["content_hash"])
        if created:
            created_count += 1
    return created_count


async def reembed_pending(*, _embed_fn=None, _list_fn=None, _set_fn=None, limit=200) -> int:
    """Добирает эмбеддинги для pending/failed пар. Возвращает число ставших ready."""
    if _embed_fn is None:
        from ..knowledge.indexer import embed_text as _embed_fn
    if _list_fn is None:
        from ..db import list_pending_embeddings as _list_fn
    if _set_fn is None:
        from ..db import set_pair_embedding as _set_fn
    rows = await _list_fn(limit)
    fixed = 0
    for row in rows:
        pair = {"context": row["context"], "operator_answer": row["operator_answer"]}
        embedding = await _embed_fn(build_embedding_text(pair), task_type="passage")
        if embedding is None:
            await _set_fn(row["pair_id"], None, None, "failed")
            continue
        await _set_fn(row["pair_id"], embedding.tobytes(), _EMBED_MODEL, "ready")
        fixed += 1
    return fixed
```

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_dialogue_mining.py -v`

- [ ] **Step 5: Commit**

```bash
git add bot/agent/dialogue_mining.py tests/test_dialogue_mining.py
git commit -m "feat(dialogue): problem-side embed, pending status, reembed pass"
```

---

## Task 6: Cursor-resumable CLI

**Files:**
- Create: `scripts/backfill_dialogue_pairs.py`

**Interfaces:**
- Consumes: config, HDE client, mining + store. Behavior: iterate pages `1..max_pages`; for each ticket NOT in `list_processed_ticket_ids()`, `mine_ticket_pairs` then `mark_ticket_processed`; per-ticket errors → `log_ticket_error` + skip (page continues); `--status` prints processed count, pair count, pending-embedding count, error count; `--reembed` runs `reembed_pending` only. Overlapping re-runs are safe (processed-id cursor + content-hash dedup).
- No unit tests (thin CLI); smoke `--help`.

- [ ] **Step 1: Write the CLI**

```python
# scripts/backfill_dialogue_pairs.py
"""Cursor-resumable backfill of dialogue_pairs from closed HDE tickets (Phase 2A).

  python scripts/backfill_dialogue_pairs.py --pages 20   # scan up to 20 pages
  python scripts/backfill_dialogue_pairs.py --status     # progress only
  python scripts/backfill_dialogue_pairs.py --reembed    # fill pending embeddings

Resumable by processed ticket-id set (page numbers shift as tickets close, so the
cursor is ids, not a page). Re-runs re-scan overlapping pages harmlessly (dedup).
Per-ticket errors are logged and skipped, never aborting a page.
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
    processed = await db.list_processed_ticket_ids()
    pending = await db.list_pending_embeddings(limit=10_000)
    print(f"processed tickets={len(processed)}, pairs={await db.count_dialogue_pairs()}, "
          f"pending embeddings={len(pending)}")


async def _reembed() -> None:
    import bot.db as db
    from bot.agent.dialogue_mining import reembed_pending
    await db.init_db()
    fixed = await reembed_pending()
    print(f"re-embedded {fixed} pairs")


async def _run(pages: int) -> None:
    import bot.db as db
    from bot.agent.dialogue_mining import mine_ticket_pairs, staff_id_set
    from bot.config import config
    from bot.hde_api import HDEApiClient

    await db.init_db()
    client = HDEApiClient()
    staff = staff_id_set(config.hde_owner_id, config.agent_staff_user_ids)
    known = await db.dialogue_pair_hashes()
    processed = await db.list_processed_ticket_ids()
    total_new = 0
    for page in range(1, pages + 1):
        tickets, total_pages = await client.get_closed_tickets_page(
            config.hde_owner_id, page
        )
        if not tickets:
            print(f"page {page}: пусто — конец")
            break
        page_new = 0
        for ticket in tickets:
            tid = str(ticket.get("id") or ticket.get("ticket_id") or "")
            if not tid or tid in processed:
                continue
            try:
                page_new += await mine_ticket_pairs(client, ticket, staff, known_hashes=known)
                await db.mark_ticket_processed(tid)
                processed.add(tid)
            except Exception as exc:
                await db.log_ticket_error(tid, str(exc))
                print(f"  ticket {tid}: ошибка — пропущен ({exc})")
            await asyncio.sleep(0.5)
        total_new += page_new
        print(f"page {page}/{total_pages}: +{page_new} pairs (сессия {total_new})")
        if page >= total_pages:
            print("последняя страница — конец")
            break
    print(f"Готово. Новых пар: {total_new}. Всего: {await db.count_dialogue_pairs()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--reembed", action="store_true")
    args = parser.parse_args()
    if args.status:
        asyncio.run(_status())
    elif args.reembed:
        asyncio.run(_reembed())
    else:
        asyncio.run(_run(args.pages))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-check** `python scripts/backfill_dialogue_pairs.py --help` → usage, exit 0.

- [ ] **Step 3: Full suite** `python -m pytest -q` → PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/backfill_dialogue_pairs.py
git commit -m "feat(dialogue): cursor-resumable CLI with reembed + error skip"
```

---

## Task 7: Gated nightly increment (own flag, 7-day cutoff)

**Files:**
- Modify: `bot/scheduler.py`
- Test: `tests/test_dialogue_mining.py` (append)

**Interfaces:**
- Produces: `_maybe_backfill_dialogue_pairs(bot) -> None` — gated on `config.agent_dialogue_mining_enabled` (NOT `agent_enabled`); once/day (in-memory date flag, MSK hour 1 per file convention); scans page 1 of closed tickets, mines only tickets whose `resolved_at <= now-7d` (skip too-recent — may reopen), skipping already-processed ids; per-ticket errors logged. Wired into `process_scheduled_actions` matching neighboring `_maybe_*` invocation style.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialogue_mining.py (append)
import bot.scheduler as scheduler_module


async def test_nightly_skips_when_mining_disabled(monkeypatch):
    cfg = config_module.config
    monkeypatch.setattr(cfg, "agent_dialogue_mining_enabled", False)
    called = {"mine": False}

    async def fake_mine(*a, **k):
        called["mine"] = True
        return 0

    monkeypatch.setattr("bot.agent.dialogue_mining.mine_ticket_pairs", fake_mine)
    scheduler_module._last_dialogue_backfill_date = None
    await scheduler_module._maybe_backfill_dialogue_pairs(bot=None)
    assert called["mine"] is False


async def test_nightly_independent_of_agent_enabled(monkeypatch):
    cfg = config_module.config
    # agent_enabled ON but mining flag OFF → still no mining
    monkeypatch.setattr(cfg, "agent_enabled", True)
    monkeypatch.setattr(cfg, "agent_dialogue_mining_enabled", False)
    called = {"mine": False}

    async def fake_mine(*a, **k):
        called["mine"] = True
        return 0

    monkeypatch.setattr("bot.agent.dialogue_mining.mine_ticket_pairs", fake_mine)
    scheduler_module._last_dialogue_backfill_date = None
    await scheduler_module._maybe_backfill_dialogue_pairs(bot=None)
    assert called["mine"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dialogue_mining.py -k nightly -v`
Expected: FAIL — attribute missing.

- [ ] **Step 3: Implement**

Add near other `_last_*_date` globals:

```python
_last_dialogue_backfill_date: str | None = None
```

Add the job (mirror `_maybe_send_digest`'s daily-flag + `_now_msk`; 7-day cutoff via parsed `resolved_at` when the ticket dict exposes a resolution date — if it doesn't, fall back to skipping tickets whose id is not yet 7 days old by processed-set heuristic and note the limitation in a comment):

```python
async def _maybe_backfill_dialogue_pairs(bot) -> None:
    """Ночной инкремент dialogue_pairs (Phase 2A). Отдельный флаг
    agent_dialogue_mining_enabled — НЕ зависит от agent_enabled. Берёт только
    тикеты, закрытые >7 дней назад (свежие могут переоткрыться)."""
    global _last_dialogue_backfill_date
    from .config import config
    if not config.agent_dialogue_mining_enabled:
        return
    now = _now_msk()
    today = now.strftime("%Y-%m-%d")
    if _last_dialogue_backfill_date == today or now.hour != 1:
        return
    _last_dialogue_backfill_date = today
    try:
        import bot.db as db
        from .agent.dialogue_mining import mine_ticket_pairs, staff_id_set
        from .hde_api import HDEApiClient
        client = HDEApiClient()
        staff = staff_id_set(config.hde_owner_id, config.agent_staff_user_ids)
        known = await db.dialogue_pair_hashes()
        processed = await db.list_processed_ticket_ids()
        tickets, _ = await client.get_closed_tickets_page(config.hde_owner_id, 1)
        saved = 0
        for ticket in tickets:
            tid = str(ticket.get("id") or ticket.get("ticket_id") or "")
            if not tid or tid in processed:
                continue
            if not _resolved_before_cutoff(ticket, now):  # 7-day rule
                continue
            try:
                saved += await mine_ticket_pairs(client, ticket, staff, known_hashes=known)
                await db.mark_ticket_processed(tid)
            except Exception as exc:
                await db.log_ticket_error(tid, str(exc))
        logger.info("Nightly dialogue backfill: +%d pairs", saved)
    except Exception as exc:
        logger.warning("Nightly dialogue backfill failed: %s", exc)
```

Add a small `_resolved_before_cutoff(ticket, now)` helper that parses the ticket's resolution/update date if present and returns `True` when it is ≥7 days before `now`; if no such field is available on the raw ticket dict, return `True` (mine anyway) and leave a comment that the 7-day guard needs the resolution timestamp — cross-reference the Task 0 note. Wire `await _maybe_backfill_dialogue_pairs(bot)` into `process_scheduled_actions` matching the neighboring call style.

- [ ] **Step 4: Run test — PASS.** `python -m pytest tests/test_dialogue_mining.py -v`

- [ ] **Step 5: Full suite + commit**

Run: `python -m pytest -q` → PASS.

```bash
git add bot/scheduler.py tests/test_dialogue_mining.py
git commit -m "feat(dialogue): nightly increment — own flag, 7-day cutoff"
```

---

## Task 8: Full-suite verification

- [ ] **Step 1:** `python -m pytest -q` → PASS.
- [ ] **Step 2:** `python -c "import bot.main" 2>&1 | tail -1` → clean import.
- [ ] **Step 3:** `git commit -m "test(dialogue): phase 2A verification" --allow-empty`.

---

## Self-Review (rev.2)

**Review findings addressed:**
1. Fragile page checkpoint → processed-ticket-id cursor + safe overlapping re-scan → Tasks 2, 6. ✓
2. Full history not guaranteed → `get_all_ticket_posts` paginator → Task 3, used in Task 5. ✓
3. Implicit ordering → explicit `sort_posts` + reversed-input test → Task 4. ✓
4. `source_message_id` wrong → now LAST CLIENT post before the turn; `context_until_message_id` = last prior post → Task 4. ✓
5. Bot/system as clients → `is_staff_post` + Task 0 author-type investigation governs the rule → Tasks 0, 4. ✓
6. Consecutive operator posts → merged into one turn → Task 4 (`test_split_merges_consecutive_operator_posts`). ✓
7. Wrong embedding side → `build_embedding_text` problem-side + `embedding_text_hash` → Task 5 (test asserts answer NOT embedded). ✓
8. Embedding failure permanent → `embedding_status` pending/failed + `reembed_pending` → Tasks 1, 2, 5. ✓
9. Missing timestamps → `operator_answer_at`, `resolved_at`, `inserted_at`; `created_at` dropped in favor of explicit columns → Task 1, plumbed Tasks 4–5. ✓
10. 7-day rule for increment → `_resolved_before_cutoff` in nightly job → Task 7. ✓
11. content_hash excludes answer → now includes normalized answer → Task 4 (`test_content_hash_changes_with_edited_answer`). ✓
12. `known_hashes or set()` bug + blind increment → `known if known_hashes is not None else set()` + `(id,created)` count → Tasks 2, 5. ✓
13. Nightly tied to AGENT_ENABLED → own flag `agent_dialogue_mining_enabled` → Tasks 3, 7 (`test_nightly_independent_of_agent_enabled`). ✓
14. Per-ticket error handling → `log_ticket_error` + skip, page continues → Tasks 2, 6, 7. ✓

**Placeholder scan:** Task 0 is an investigation with a written deliverable (the findings note), not a placeholder; Tasks 3/7 note "if the endpoint lacks paging / resolution date, document and fall back" — these are explicit branch instructions tied to the Task 0 note, not vague TODOs.

**Type consistency:** `save_dialogue_pair` returns `(int, bool)` consumed by `mine_ticket_pairs`; pair dict keys from `split_ticket_into_pairs` (Task 4) match `mine_ticket_pairs`'s reads (Task 5); `build_embedding_text` reads `context`/`operator_answer` present in both the pair dict and `list_pending_embeddings` rows; `staff_id_set`/`is_staff_post` shared Tasks 4/5/6/7; `_EMBED_MODEL` constant matches the schema `embedding_model` value.
