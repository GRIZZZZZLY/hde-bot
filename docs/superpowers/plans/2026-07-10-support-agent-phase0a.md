# Support Agent — Phase 0A (Tracing Infrastructure) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the online tracing layer (`ai_suggestions` + `ai_suggestion_events`), idempotency, freshness, feature flags, and the rule-based safety module — so every AI suggestion is fully traced and events are recorded from day one, before the agent pipeline of Phase 1 exists.

**Architecture:** New DB tables recorded from the existing generation path (`register_feedback_pending`) and updated by the existing feedback buttons (`bot/handlers/ai_feedback.py`). Two independent-dimension status columns, an append-only event log, and a `human_label` derived from the full event chain. Safety and freshness live in a new `bot/agent/` package that Phase 1 will build the pipeline on top of. All tracing writes are **non-fatal** (wrapped in try/except) so they never break the current feedback flow.

**Tech Stack:** Python 3, aiosqlite, aiogram, pytest (`asyncio_mode = auto`).

## Global Constraints

- No new third-party dependencies (stdlib `hashlib`, `re`, `json` only).
- Match existing code style: `from __future__ import annotations`; RU comments where the file uses them; store functions live in `bot/db/*_store.py` and are re-exported from `bot/db/__init__.py`.
- All tracing writes MUST be non-fatal: wrap every new `ai_suggestions` / `ai_suggestion_events` call in the caller with `try/except` + `logger.warning`, exactly like the existing `save_optimization_sample` calls in `bot/handlers/ai_feedback.py`.
- DB tables are created in `bot/db/core.py::init_db` with `CREATE TABLE IF NOT EXISTS`; column additions to existing tables use the `try/except ALTER TABLE` migration pattern already in that file. **Do NOT alter `ai_feedback_pending`.**
- Tests: `asyncio_mode = auto` (plain `async def test_...`, no marker needed); use the `set_test_db` autouse fixture (already in `tests/conftest.py`) — call `await bot.db.init_db()` at the top of each DB test.
- Feature flags default to **False / off** so this phase changes no runtime behavior until explicitly enabled.
- `idempotency_key` includes `prompt_version` (refinement 1): any prompt change is captured in the key even if `pipeline_version` is not bumped.
- `human_label` is derived from the **full event set**, never the last event (refinement 2).
- `client_id` is an opportunistic column on `ai_suggestions` (refinement 3): populated when the caller has it, else NULL. Phase 4 `ticket_transcripts` will carry its own `client_id` resolved via `get_ticket_info(ticket_id)`; client-scoped retrieval filters by `client_id`. This rule is locked here; the transcript table itself is out of Phase 0 scope.

---

## Out of scope for this plan (Phase 0B — separate plan)

Golden set builder, contamination `ticket_id` exclusion list, and the baseline eval harness. These form a distinct offline-eval subsystem built on the existing `scripts/eval_prompt.py` + `bot/optimizer/judge.py`, and get their own plan after this one lands.

---

## File Structure

- Create: `bot/db/suggestion_store.py` — schema-free store for `ai_suggestions` / `ai_suggestion_events`; pure helpers `compute_idempotency_key`, `derive_human_label`.
- Create: `bot/agent/__init__.py` — package marker (Phase 1 fills the pipeline).
- Create: `bot/agent/safety.py` — `pre_generation_policy_check`, `post_generation_safety_check`, `PolicyDecision`.
- Create: `bot/agent/freshness.py` — `check_freshness`.
- Modify: `bot/db/core.py` — add two `CREATE TABLE` blocks + indexes in `init_db`.
- Modify: `bot/db/__init__.py` — re-export new store functions.
- Modify: `bot/config.py` — add five agent feature flags.
- Modify: `bot/handlers/ai_feedback.py` — record events on button actions.
- Modify: `bot/handlers/ai_feedback.py::register_feedback_pending` — record the suggestion row at generation time.
- Test: `tests/test_suggestion_store.py`, `tests/test_agent_safety.py`, `tests/test_agent_freshness.py`, `tests/test_ai_feedback_events.py`, `tests/test_agent_flags.py`.

---

## Task 1: Schema — `ai_suggestions` + `ai_suggestion_events`

**Files:**
- Modify: `bot/db/core.py` (inside `init_db`, before the final `await db.commit()` at line ~494)
- Test: `tests/test_suggestion_store.py`

**Interfaces:**
- Produces: tables `ai_suggestions` and `ai_suggestion_events` with the columns used by all later tasks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_suggestion_store.py
import bot.db as db_module


async def _columns(table: str) -> set[str]:
    from bot.db.core import connect
    async with connect() as db:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
    return {r[1] for r in rows}


async def test_ai_suggestions_table_created():
    await db_module.init_db()
    cols = await _columns("ai_suggestions")
    expected = {
        "id", "ticket_id", "topic_id", "trigger_source", "context_until_post_id",
        "client_id", "idempotency_key", "title", "history", "client_text",
        "retrieval_query", "retrieval_config_version", "embedding_model",
        "retrieved_refs", "pipeline_version", "prompt_version", "model",
        "action_type", "ai_answer", "ai_full_text", "confidence",
        "confidence_reason", "self_check", "generation_status", "review_status",
        "delivery_status", "evaluation_status", "freshness_status",
        "final_sent_text", "final_sent_post_id", "reviewed_at", "sent_at",
        "human_label", "judge_label", "judge_detail", "judge_reference_answer",
        "judged_at", "effective_label", "generation_ms", "tokens_in",
        "tokens_out", "error", "created_at",
    }
    assert expected <= cols


async def test_ai_suggestion_events_table_created():
    await db_module.init_db()
    cols = await _columns("ai_suggestion_events")
    assert {"id", "suggestion_id", "event_type", "payload", "hde_post_id", "created_at"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_suggestion_store.py -v`
Expected: FAIL — `no such table: ai_suggestions`.

- [ ] **Step 3: Add the tables in `init_db`**

In `bot/db/core.py`, immediately before the closing `await db.commit()` of `init_db` (currently line ~494), insert:

```python
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_suggestions (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id                TEXT NOT NULL,
                topic_id                 INTEGER,
                trigger_source           TEXT NOT NULL DEFAULT 'first',
                context_until_post_id    TEXT,
                client_id                TEXT,
                idempotency_key          TEXT UNIQUE,
                title                    TEXT DEFAULT '',
                history                  TEXT NOT NULL DEFAULT '',
                client_text              TEXT DEFAULT '',
                retrieval_query          TEXT,
                retrieval_config_version TEXT,
                embedding_model          TEXT,
                retrieved_refs           TEXT,
                pipeline_version         TEXT,
                prompt_version           TEXT,
                model                    TEXT,
                action_type              TEXT,
                ai_answer                TEXT DEFAULT '',
                ai_full_text             TEXT DEFAULT '',
                confidence               INTEGER,
                confidence_reason        TEXT,
                self_check               TEXT,
                generation_status        TEXT NOT NULL DEFAULT 'completed',
                review_status            TEXT NOT NULL DEFAULT 'pending',
                delivery_status          TEXT NOT NULL DEFAULT 'not_sent',
                evaluation_status        TEXT NOT NULL DEFAULT 'pending',
                freshness_status         TEXT NOT NULL DEFAULT 'current',
                final_sent_text          TEXT,
                final_sent_post_id       TEXT,
                reviewed_at              TEXT,
                sent_at                  TEXT,
                human_label              TEXT,
                judge_label              TEXT,
                judge_detail             TEXT,
                judge_reference_answer   TEXT,
                judged_at                TEXT,
                effective_label          TEXT,
                generation_ms            INTEGER,
                tokens_in                INTEGER,
                tokens_out               INTEGER,
                error                    TEXT,
                created_at               TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_suggestions_topic ON ai_suggestions(topic_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_suggestions_eval "
            "ON ai_suggestions(evaluation_status)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_suggestion_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_id INTEGER NOT NULL,
                event_type    TEXT NOT NULL,
                payload       TEXT,
                hde_post_id   TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_suggestion_events_sid "
            "ON ai_suggestion_events(suggestion_id)"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_suggestion_store.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/db/core.py tests/test_suggestion_store.py
git commit -m "feat(agent): ai_suggestions + ai_suggestion_events schema"
```

---

## Task 2: Pure helpers — idempotency key + human_label derivation

**Files:**
- Create: `bot/db/suggestion_store.py`
- Test: `tests/test_suggestion_store.py` (append)

**Interfaces:**
- Produces:
  - `compute_idempotency_key(ticket_id: str, trigger_source: str, context_until_post_id: str | None, pipeline_version: str | None, prompt_version: str | None) -> str`
  - `derive_human_label(event_types: list[str]) -> str | None` returning `"accepted" | "corrected" | "rejected" | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_suggestion_store.py (append)
from bot.db.suggestion_store import compute_idempotency_key, derive_human_label


def test_idempotency_key_stable_and_includes_prompt_version():
    a = compute_idempotency_key("T1", "first", "99", "v0", "legacy")
    b = compute_idempotency_key("T1", "first", "99", "v0", "legacy")
    assert a == b
    # refinement 1: prompt_version change alone yields a different key
    c = compute_idempotency_key("T1", "first", "99", "v0", "legacy-2")
    assert a != c


def test_derive_human_label_full_chain():
    assert derive_human_label(["approved"]) == "accepted"
    assert derive_human_label(["send_requested", "sent"]) == "accepted"
    assert derive_human_label(["edit_started", "edited", "send_requested", "sent"]) == "corrected"
    assert derive_human_label(["edit_started", "edited"]) == "corrected"
    assert derive_human_label(["rejected"]) == "rejected"
    assert derive_human_label(["send_requested", "send_failed"]) is None
    assert derive_human_label([]) is None


def test_derive_human_label_sent_dominates_last_event():
    # full-chain, not last-event: approved then sent stays accepted
    assert derive_human_label(["approved", "send_requested", "sent"]) == "accepted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_suggestion_store.py -k "idempotency or human_label" -v`
Expected: FAIL — `No module named 'bot.db.suggestion_store'`.

- [ ] **Step 3: Create the module with pure helpers**

```python
# bot/db/suggestion_store.py
"""Store + pure helpers for ai_suggestions and ai_suggestion_events (Phase 0A tracing)."""
from __future__ import annotations

import hashlib

import aiosqlite

from .core import connect


def compute_idempotency_key(
    ticket_id: str,
    trigger_source: str,
    context_until_post_id: str | None,
    pipeline_version: str | None,
    prompt_version: str | None,
) -> str:
    """Deterministic key. Includes prompt_version so a prompt change is captured
    even when pipeline_version is not bumped (refinement 1)."""
    raw = "|".join(
        [
            str(ticket_id),
            str(trigger_source),
            str(context_until_post_id or ""),
            str(pipeline_version or ""),
            str(prompt_version or ""),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def derive_human_label(event_types: list[str]) -> str | None:
    """Итог по полной цепочке событий, не по последнему (refinement 2).

    sent → accepted (или corrected, если была правка); rejected → rejected;
    approved → accepted; edited без отправки → corrected; иначе None.
    """
    s = set(event_types)
    if "sent" in s:
        return "corrected" if "edited" in s else "accepted"
    if "rejected" in s:
        return "rejected"
    if "approved" in s:
        return "accepted"
    if "edited" in s:
        return "corrected"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_suggestion_store.py -k "idempotency or human_label" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/db/suggestion_store.py tests/test_suggestion_store.py
git commit -m "feat(agent): idempotency key + human_label derivation helpers"
```

---

## Task 3: Store — record + read suggestions

**Files:**
- Modify: `bot/db/suggestion_store.py`
- Modify: `bot/db/__init__.py`
- Test: `tests/test_suggestion_store.py` (append)

**Interfaces:**
- Consumes: `compute_idempotency_key` (Task 2), tables (Task 1).
- Produces:
  - `record_suggestion(*, ticket_id, topic_id, trigger_source, context_until_post_id, pipeline_version, prompt_version, title="", history="", client_text="", ai_answer="", ai_full_text="", client_id=None, model=None) -> int` — returns suggestion id; idempotent on the key (returns the existing id on repeat).
  - `get_suggestion(suggestion_id: int) -> dict | None`
  - `get_open_suggestion_by_topic(topic_id: int) -> dict | None` — most recent row for the topic.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_suggestion_store.py (append)
import bot.db as db_module
from bot.db.suggestion_store import (
    get_open_suggestion_by_topic,
    get_suggestion,
    record_suggestion,
)


async def test_record_and_read_suggestion():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="T1", topic_id=555, trigger_source="first",
        context_until_post_id="99", pipeline_version="v0", prompt_version="legacy",
        title="Тема", history="диалог", ai_full_text="Клиенту: ...",
    )
    row = await get_suggestion(sid)
    assert row["ticket_id"] == "T1"
    assert row["topic_id"] == 555
    assert row["review_status"] == "pending"
    assert row["delivery_status"] == "not_sent"
    assert row["evaluation_status"] == "pending"
    assert row["freshness_status"] == "current"


async def test_record_suggestion_idempotent():
    await db_module.init_db()
    kw = dict(
        ticket_id="T2", topic_id=1, trigger_source="first",
        context_until_post_id="10", pipeline_version="v0", prompt_version="legacy",
    )
    first = await record_suggestion(**kw)
    again = await record_suggestion(**kw)
    assert first == again  # same key → same row, no duplicate


async def test_get_open_suggestion_returns_latest():
    await db_module.init_db()
    await record_suggestion(
        ticket_id="T3", topic_id=7, trigger_source="first",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
    )
    second = await record_suggestion(
        ticket_id="T3", topic_id=7, trigger_source="button",
        context_until_post_id="2", pipeline_version="v0", prompt_version="legacy",
    )
    row = await get_open_suggestion_by_topic(7)
    assert row["id"] == second
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_suggestion_store.py -k "record or open_suggestion" -v`
Expected: FAIL — `cannot import name 'record_suggestion'`.

- [ ] **Step 3: Implement store functions**

Append to `bot/db/suggestion_store.py`:

```python
async def record_suggestion(
    *,
    ticket_id: str,
    topic_id: int | None,
    trigger_source: str,
    context_until_post_id: str | None,
    pipeline_version: str | None,
    prompt_version: str | None,
    title: str = "",
    history: str = "",
    client_text: str = "",
    ai_answer: str = "",
    ai_full_text: str = "",
    client_id: str | None = None,
    model: str | None = None,
) -> int:
    """Insert a suggestion row (idempotent on idempotency_key). Returns its id."""
    key = compute_idempotency_key(
        ticket_id, trigger_source, context_until_post_id, pipeline_version, prompt_version
    )
    async with connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_suggestions "
            "(ticket_id, topic_id, trigger_source, context_until_post_id, client_id, "
            " idempotency_key, title, history, client_text, ai_answer, ai_full_text, "
            " pipeline_version, prompt_version, model) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_id, topic_id, trigger_source, context_until_post_id, client_id,
                key, title, history, client_text, ai_answer, ai_full_text,
                pipeline_version, prompt_version, model,
            ),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM ai_suggestions WHERE idempotency_key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def get_suggestion(suggestion_id: int) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ai_suggestions WHERE id=?", (suggestion_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_open_suggestion_by_topic(topic_id: int) -> dict | None:
    """Most recent suggestion for a topic — the one the feedback buttons act on
    (ai_feedback_pending is single-per-topic, so latest == the pending one)."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ai_suggestions WHERE topic_id=? ORDER BY id DESC LIMIT 1",
            (topic_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None
```

Add to `bot/db/__init__.py` a new import block (after the `optimizer_store` import block, ~line 132):

```python
from .suggestion_store import (
    compute_idempotency_key,
    derive_human_label,
    get_open_suggestion_by_topic,
    get_suggestion,
    record_suggestion,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_suggestion_store.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add bot/db/suggestion_store.py bot/db/__init__.py tests/test_suggestion_store.py
git commit -m "feat(agent): record_suggestion store + reads, wired into db facade"
```

---

## Task 4: Store — events + label recomputation + denormalization

**Files:**
- Modify: `bot/db/suggestion_store.py`
- Modify: `bot/db/__init__.py`
- Test: `tests/test_suggestion_store.py` (append)

**Interfaces:**
- Consumes: `record_suggestion`, `get_suggestion`, `derive_human_label`.
- Produces:
  - `record_suggestion_event(suggestion_id: int, event_type: str, *, payload: str | None = None, hde_post_id: str | None = None) -> int`
  - denormalization: `review_status` / `delivery_status` / `final_sent_text` / `final_sent_post_id` / `sent_at` / `reviewed_at` updated; `human_label` + `effective_label` recomputed from the full event chain after every event.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_suggestion_store.py (append)
from bot.db.suggestion_store import record_suggestion_event


async def _new_suggestion(topic_id=1, ctx="1") -> int:
    return await record_suggestion(
        ticket_id="T", topic_id=topic_id, trigger_source="first",
        context_until_post_id=ctx, pipeline_version="v0", prompt_version="legacy",
    )


async def test_event_approved_sets_review_and_label():
    await db_module.init_db()
    sid = await _new_suggestion()
    await record_suggestion_event(sid, "approved")
    row = await get_suggestion(sid)
    assert row["review_status"] == "approved"
    assert row["human_label"] == "accepted"
    assert row["effective_label"] == "accepted"


async def test_event_edited_then_sent_is_corrected():
    await db_module.init_db()
    sid = await _new_suggestion()
    await record_suggestion_event(sid, "edit_started")
    await record_suggestion_event(sid, "edited", payload="исправленный текст")
    await record_suggestion_event(sid, "send_requested")
    await record_suggestion_event(sid, "sent", payload="исправленный текст", hde_post_id="4321")
    row = await get_suggestion(sid)
    assert row["review_status"] == "edited"
    assert row["delivery_status"] == "sent"
    assert row["final_sent_text"] == "исправленный текст"
    assert row["final_sent_post_id"] == "4321"
    assert row["human_label"] == "corrected"


async def test_event_send_failed_leaves_label_none():
    await db_module.init_db()
    sid = await _new_suggestion()
    await record_suggestion_event(sid, "send_requested")
    await record_suggestion_event(sid, "send_failed", payload="HDE 500")
    row = await get_suggestion(sid)
    assert row["delivery_status"] == "failed"
    assert row["human_label"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_suggestion_store.py -k "event_" -v`
Expected: FAIL — `cannot import name 'record_suggestion_event'`.

- [ ] **Step 3: Implement events + recomputation**

Append to `bot/db/suggestion_store.py`:

```python
_REVIEW_BY_EVENT = {"approved": "approved", "rejected": "rejected", "edited": "edited"}
_DELIVERY_BY_EVENT = {"send_requested": "requested", "sent": "sent", "send_failed": "failed"}


async def record_suggestion_event(
    suggestion_id: int,
    event_type: str,
    *,
    payload: str | None = None,
    hde_post_id: str | None = None,
) -> int:
    """Append an operator-action event and update denormalized status + labels."""
    async with connect() as db:
        cursor = await db.execute(
            "INSERT INTO ai_suggestion_events "
            "(suggestion_id, event_type, payload, hde_post_id) VALUES (?,?,?,?)",
            (suggestion_id, event_type, payload, hde_post_id),
        )
        if event_type in _REVIEW_BY_EVENT:
            await db.execute(
                "UPDATE ai_suggestions SET review_status=?, reviewed_at=datetime('now') WHERE id=?",
                (_REVIEW_BY_EVENT[event_type], suggestion_id),
            )
        if event_type in _DELIVERY_BY_EVENT:
            await db.execute(
                "UPDATE ai_suggestions SET delivery_status=? WHERE id=?",
                (_DELIVERY_BY_EVENT[event_type], suggestion_id),
            )
        if event_type == "sent":
            await db.execute(
                "UPDATE ai_suggestions "
                "SET final_sent_text=?, final_sent_post_id=?, sent_at=datetime('now') WHERE id=?",
                (payload or "", hde_post_id, suggestion_id),
            )
        await db.commit()
        event_id = int(cursor.lastrowid)
    await _recompute_labels(suggestion_id)
    return event_id


async def _recompute_labels(suggestion_id: int) -> None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT event_type FROM ai_suggestion_events WHERE suggestion_id=? ORDER BY id",
            (suggestion_id,),
        ) as cur:
            events = [r["event_type"] for r in await cur.fetchall()]
        async with db.execute(
            "SELECT judge_label FROM ai_suggestions WHERE id=?", (suggestion_id,)
        ) as cur:
            row = await cur.fetchone()
        judge_label = row["judge_label"] if row else None
        human = derive_human_label(events)
        effective = human if human is not None else judge_label
        await db.execute(
            "UPDATE ai_suggestions SET human_label=?, effective_label=? WHERE id=?",
            (human, effective, suggestion_id),
        )
        await db.commit()
```

Add `record_suggestion_event` to the `from .suggestion_store import (...)` block in `bot/db/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_suggestion_store.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add bot/db/suggestion_store.py bot/db/__init__.py tests/test_suggestion_store.py
git commit -m "feat(agent): suggestion events + full-chain human_label recompute"
```

---

## Task 5: Feature flags + kill switch

**Files:**
- Modify: `bot/config.py` (dataclass fields ~line 60; `from_env` ~line 101)
- Test: `tests/test_agent_flags.py`

**Interfaces:**
- Produces: `config.agent_enabled: bool`, `config.agent_pipeline_version: str`, `config.agent_auto_first_suggestion_enabled: bool`, `config.agent_dynamic_fewshot_enabled: bool`, `config.agent_call_fixation_enabled: bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_flags.py
import bot.config as config_module


def test_agent_flags_present_with_safe_defaults():
    cfg = config_module.config
    # kill switch and feature gates exist and default off (no behavior change)
    assert hasattr(cfg, "agent_enabled")
    assert hasattr(cfg, "agent_pipeline_version")
    assert hasattr(cfg, "agent_auto_first_suggestion_enabled")
    assert hasattr(cfg, "agent_dynamic_fewshot_enabled")
    assert hasattr(cfg, "agent_call_fixation_enabled")
    assert isinstance(cfg.agent_pipeline_version, str) and cfg.agent_pipeline_version


def test_agent_enabled_parses_env(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_PIPELINE_VERSION", "v1")
    fresh = config_module.Config.from_env()
    assert fresh.agent_enabled is True
    assert fresh.agent_pipeline_version == "v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_flags.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'agent_enabled'`.

- [ ] **Step 3: Add the fields**

In `bot/config.py`, add to the `Config` dataclass (after `reassurance_text: str`, line ~62):

```python
    agent_enabled: bool
    agent_pipeline_version: str
    agent_auto_first_suggestion_enabled: bool
    agent_dynamic_fewshot_enabled: bool
    agent_call_fixation_enabled: bool
```

In `from_env`, add to the `cls(...)` call (after the `reassurance_text=...` entry, line ~107):

```python
            agent_enabled=_parse_bool(os.getenv("AGENT_ENABLED"), default=False),
            agent_pipeline_version=os.getenv("AGENT_PIPELINE_VERSION", "v0").strip() or "v0",
            agent_auto_first_suggestion_enabled=_parse_bool(
                os.getenv("AGENT_AUTO_FIRST_SUGGESTION_ENABLED"), default=False
            ),
            agent_dynamic_fewshot_enabled=_parse_bool(
                os.getenv("AGENT_DYNAMIC_FEWSHOT_ENABLED"), default=False
            ),
            agent_call_fixation_enabled=_parse_bool(
                os.getenv("AGENT_CALL_FIXATION_ENABLED"), default=False
            ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_flags.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/config.py tests/test_agent_flags.py
git commit -m "feat(agent): feature flags + kill switch (default off)"
```

---

## Task 6: Safety module — pre/post policy check

**Files:**
- Create: `bot/agent/__init__.py`
- Create: `bot/agent/safety.py`
- Test: `tests/test_agent_safety.py`

**Interfaces:**
- Produces:
  - `PolicyDecision` dataclass: `action: str` (`"PROCEED" | "ESCALATE"`), `category: str | None`, `matched: str | None`.
  - `pre_generation_policy_check(text: str) -> PolicyDecision`
  - `post_generation_safety_check(answer_text: str) -> PolicyDecision`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_safety.py
from bot.agent.safety import (
    PolicyDecision,
    post_generation_safety_check,
    pre_generation_policy_check,
)


def test_diagnosis_and_explanation_proceed():
    # объяснение/диагностика допустимы — не эскалируем
    for txt in [
        "что означает ошибка ОФД 231 на кассе",
        "почему не печатается чек, что проверить",
        "касса не подключается к интернету",
    ]:
        d = pre_generation_policy_check(txt)
        assert d.action == "PROCEED", txt


def test_escalation_categories():
    cases = {
        "нужно вернуть деньги клиенту за отменённый заказ": "finance",
        "надо перерегистрировать ККТ на нового владельца": "fiscal_change",
        "удалите все товары и продажи из базы": "data_loss",
        "смените пароль клиенту и выдайте доступ": "access",
    }
    for txt, category in cases.items():
        d = pre_generation_policy_check(txt)
        assert d.action == "ESCALATE", txt
        assert d.category == category, txt


def test_post_check_scans_generated_answer():
    d = post_generation_safety_check("Я сделаю возврат средств на вашу карту.")
    assert d.action == "ESCALATE"
    assert d.category == "finance"
    assert isinstance(d, PolicyDecision)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_safety.py -v`
Expected: FAIL — `No module named 'bot.agent'`.

- [ ] **Step 3: Create the package + safety module**

```python
# bot/agent/__init__.py
"""Support agent package. Phase 0A: safety + freshness primitives.
Phase 1 builds the generation pipeline on top of these."""
```

```python
# bot/agent/safety.py
"""Rule-based safety gate. The ESCALATE decision is made by CODE, not by a
prompt instruction — that is what makes it a hard gate (spec Phase 0).

Only actions-with-consequences escalate; explanation/diagnosis proceeds.
The rule lists are a starter set, tuned as real cases arrive."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class PolicyDecision:
    action: str            # "PROCEED" | "ESCALATE"
    category: str | None   # matched escalation category, or None
    matched: str | None    # matched pattern, or None


# (category, [regex patterns]) — matched case-insensitively against lowered text.
_ESCALATION_RULES: list[tuple[str, list[str]]] = [
    ("finance", [
        r"верн\w*\s+деньг", r"возврат\w*\s+средств", r"сдела\w*\s+возврат",
        r"измен\w*\s+тариф", r"пересчита\w*\s+оплат",
    ]),
    ("fiscal_change", [
        r"перерегистр\w*\s+(?:ккт|касс|фн)", r"смен\w*\s+офд", r"замен\w*\s+фн",
    ]),
    ("data_loss", [
        r"удали\w*\s+(?:все\s+)?(?:данные|базу|товары|продажи)",
        r"сброс\w*\s+до\s+заводск", r"переустанов\w*\b.*потер",
    ]),
    ("access", [
        r"смен\w*\s+парол", r"выда\w*\s+доступ", r"перенос\w*\s+аккаунт",
        r"восстанов\w*\s+доступ",
    ]),
]


def pre_generation_policy_check(text: str) -> PolicyDecision:
    """Scan the incoming request. Escalation-category intent → ESCALATE."""
    low = (text or "").lower()
    for category, patterns in _ESCALATION_RULES:
        for pat in patterns:
            if re.search(pat, low):
                return PolicyDecision(action="ESCALATE", category=category, matched=pat)
    return PolicyDecision(action="PROCEED", category=None, matched=None)


def post_generation_safety_check(answer_text: str) -> PolicyDecision:
    """Second pass over the MODEL'S answer: if the generated text itself proposes
    an escalation-category action, force ESCALATE regardless of chosen action_type."""
    return pre_generation_policy_check(answer_text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_safety.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/agent/__init__.py bot/agent/safety.py tests/test_agent_safety.py
git commit -m "feat(agent): rule-based pre/post safety policy gate"
```

---

## Task 7: Freshness check

**Files:**
- Create: `bot/agent/freshness.py`
- Test: `tests/test_agent_freshness.py`

**Interfaces:**
- Consumes: `bot.hde_api.HDEApiClient.get_ticket_posts(ticket_id) -> list[HDEPost]` (each `HDEPost` has `.post_id`).
- Produces: `check_freshness(ticket_id: str, context_until_post_id: str | None, *, client=None) -> str` returning `"current" | "superseded"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_freshness.py
from types import SimpleNamespace

from bot.agent.freshness import check_freshness


class _FakeClient:
    def __init__(self, post_ids):
        self._posts = [SimpleNamespace(post_id=p) for p in post_ids]

    async def get_ticket_posts(self, ticket_id):
        return self._posts


async def test_current_when_latest_matches_anchor():
    client = _FakeClient([10, 20, 30])
    assert await check_freshness("T", "30", client=client) == "current"


async def test_superseded_when_new_post_arrived():
    client = _FakeClient([10, 20, 30, 31])
    assert await check_freshness("T", "30", client=client) == "superseded"


async def test_no_posts_treated_as_current():
    client = _FakeClient([])
    assert await check_freshness("T", "30", client=client) == "current"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_freshness.py -v`
Expected: FAIL — `No module named 'bot.agent.freshness'`.

- [ ] **Step 3: Implement**

```python
# bot/agent/freshness.py
"""Freshness guard: before publishing a suggestion, verify the ticket has not
advanced since the anchor post the suggestion was built on (spec Phase 0)."""
from __future__ import annotations


async def check_freshness(
    ticket_id: str,
    context_until_post_id: str | None,
    *,
    client=None,
) -> str:
    """Return 'current' if the newest HDE post equals the anchor, else 'superseded'.

    No posts (or unreadable ids) → 'current' (nothing to supersede)."""
    if client is None:
        from ..hde_api import HDEApiClient
        client = HDEApiClient()
    posts = await client.get_ticket_posts(ticket_id)
    if not posts:
        return "current"
    try:
        latest = max(int(p.post_id) for p in posts)
    except (TypeError, ValueError):
        return "current"
    if context_until_post_id is None:
        return "superseded"
    try:
        anchor = int(context_until_post_id)
    except (TypeError, ValueError):
        return "current"
    return "current" if latest <= anchor else "superseded"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_freshness.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/agent/freshness.py tests/test_agent_freshness.py
git commit -m "feat(agent): freshness check against latest HDE post"
```

---

## Task 8: Wire events into feedback buttons

**Files:**
- Modify: `bot/handlers/ai_feedback.py`
- Test: `tests/test_ai_feedback_events.py`

**Interfaces:**
- Consumes: `get_open_suggestion_by_topic`, `record_suggestion_event` (Tasks 3–4).
- Produces: a private helper `_record_event(topic_id, event_type, *, payload=None, hde_post_id=None)` used by the callbacks; each existing callback additionally records its event. Existing `save_optimization_sample` calls are left untouched.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ai_feedback_events.py
import bot.db as db_module
from bot.db.suggestion_store import get_suggestion, record_suggestion
from bot.handlers.ai_feedback import _record_event


async def test_record_event_maps_topic_to_latest_suggestion():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="T", topic_id=42, trigger_source="first",
        context_until_post_id="5", pipeline_version="v0", prompt_version="legacy",
    )
    await _record_event(42, "approved")
    row = await get_suggestion(sid)
    assert row["review_status"] == "approved"
    assert row["human_label"] == "accepted"


async def test_record_event_noop_without_suggestion():
    await db_module.init_db()
    # no suggestion for this topic — must not raise
    await _record_event(999, "approved")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ai_feedback_events.py -v`
Expected: FAIL — `cannot import name '_record_event'`.

- [ ] **Step 3: Add helper + call it from callbacks**

In `bot/handlers/ai_feedback.py`, extend the db import (line ~11) to include the new functions:

```python
from ..db import (
    delete_ai_feedback_pending,
    get_ai_feedback_pending,
    get_open_suggestion_by_topic,
    record_suggestion_event,
    save_ai_feedback_pending,
)
```

Add the helper after `register_feedback_pending` (after line ~83):

```python
async def _record_event(
    topic_id: int,
    event_type: str,
    *,
    payload: str | None = None,
    hde_post_id: str | None = None,
) -> None:
    """Non-fatal: attach an operator-action event to the topic's latest suggestion."""
    try:
        suggestion = await get_open_suggestion_by_topic(topic_id)
        if suggestion is None:
            return
        await record_suggestion_event(
            suggestion["id"], event_type, payload=payload, hde_post_id=hde_post_id
        )
    except Exception as exc:
        logger.warning("ai_feedback: event record failed (%s): %s", event_type, exc)
```

Then add one `_record_event` call inside each callback, right after `topic_id` is resolved:

- `cb_ai_good` (after `topic_id = callback.message.message_thread_id`, line ~93): `await _record_event(topic_id, "approved")`
- `cb_ai_bad` (line ~142): `await _record_event(topic_id, "rejected")`
- `cb_ai_edit` (line ~172): `await _record_event(topic_id, "edit_started")`
- `capture_correction` (after `correction_text = message.text.strip()`, line ~303): `await _record_event(topic_id, "edited", payload=correction_text)`
- `cb_send_to_hde`: after `topic_id = callback.message.message_thread_id` (line ~244) add `await _record_event(topic_id, "send_requested")`; on success (after line ~267 `await callback.answer(...)`) add `await _record_event(topic_id, "sent", payload=answer_text)`; in the `except HDEApiError` branch (line ~263) add `await _record_event(topic_id, "send_failed", payload=str(exc))` before `return`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ai_feedback_events.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full feedback test suite (no regression)**

Run: `python -m pytest tests/test_ai_feedback_flow.py tests/test_ai_feedback_events.py -v`
Expected: PASS (existing feedback flow unchanged).

- [ ] **Step 6: Commit**

```bash
git add bot/handlers/ai_feedback.py tests/test_ai_feedback_events.py
git commit -m "feat(agent): record operator-action events from feedback buttons"
```

---

## Task 9: Record the suggestion at generation time

**Files:**
- Modify: `bot/handlers/ai_feedback.py::register_feedback_pending`
- Test: `tests/test_ai_feedback_events.py` (append)

**Interfaces:**
- Consumes: `record_suggestion` (Task 3), `config` feature flags (Task 5).
- Produces: `register_feedback_pending` gains optional kwargs `trigger_source="first"`, `context_until_post_id=None`, `client_id=None`, and — non-fatally — records an `ai_suggestions` row when the pending state is saved. Signature stays backward-compatible (all new args keyword-optional).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ai_feedback_events.py (append)
from bot.db.suggestion_store import get_open_suggestion_by_topic
from bot.handlers.ai_feedback import register_feedback_pending


async def test_register_feedback_pending_records_suggestion():
    await db_module.init_db()
    await register_feedback_pending(
        topic_id=77, ticket_id="T77", history="диалог клиента",
        title="Не печатает чек", answer_text="Клиенту: проверьте бумагу",
        ai_full_text="Суть: ...\nКлиенту: проверьте бумагу",
        context_until_post_id="123",
    )
    row = await get_open_suggestion_by_topic(77)
    assert row is not None
    assert row["ticket_id"] == "T77"
    assert row["trigger_source"] == "first"
    assert row["context_until_post_id"] == "123"
    assert row["ai_full_text"].endswith("проверьте бумагу")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ai_feedback_events.py -k register_feedback -v`
Expected: FAIL — `register_feedback_pending() got an unexpected keyword argument 'context_until_post_id'`.

- [ ] **Step 3: Extend `register_feedback_pending`**

Extend the db import in `bot/handlers/ai_feedback.py` (the block edited in Task 8) to also include `record_suggestion`:

```python
from ..db import (
    delete_ai_feedback_pending,
    get_ai_feedback_pending,
    get_open_suggestion_by_topic,
    record_suggestion,
    record_suggestion_event,
    save_ai_feedback_pending,
)
```

Replace the body of `register_feedback_pending` (lines ~69–83) with:

```python
async def register_feedback_pending(
    topic_id: int,
    ticket_id: str,
    history: str,
    title: str,
    answer_text: str = "",
    ai_full_text: str = "",
    *,
    trigger_source: str = "first",
    context_until_post_id: str | None = None,
    client_id: str | None = None,
) -> None:
    """Store pending feedback state so correction handler can pick it up, and
    (non-fatally) record the suggestion row for tracing."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_TTL_HOURS)
    ).isoformat()
    await save_ai_feedback_pending(
        topic_id, ticket_id, history, title, expires_at, answer_text, ai_full_text
    )
    try:
        from ..config import config
        await record_suggestion(
            ticket_id=ticket_id,
            topic_id=topic_id,
            trigger_source=trigger_source,
            context_until_post_id=context_until_post_id,
            pipeline_version=config.agent_pipeline_version,
            prompt_version="legacy",  # Phase 0 still uses _FORMAT_INSTRUCTIONS
            title=title,
            history=history,
            ai_answer=answer_text,
            ai_full_text=ai_full_text,
            client_id=client_id,
        )
    except Exception as exc:
        logger.warning("ai_feedback: suggestion record failed: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ai_feedback_events.py -v`
Expected: PASS (all).

- [ ] **Step 5: Full regression run**

Run: `python -m pytest tests/test_ai_feedback_flow.py tests/test_topic_history.py -v`
Expected: PASS — existing callers of `register_feedback_pending` (in `bot/topic_history.py`) still work; new kwargs are optional.

Note: `bot/topic_history.py` call sites may pass `trigger_source`/`context_until_post_id` later; the default `trigger_source="first"` keeps the existing first-suggestion path correct without edits. Wiring the anchor post id from `topic_history` is a Phase 1 refinement.

- [ ] **Step 6: Commit**

```bash
git add bot/handlers/ai_feedback.py tests/test_ai_feedback_events.py
git commit -m "feat(agent): record ai_suggestions row at generation time"
```

---

## Task 10: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -q`
Expected: PASS — all existing tests plus the five new files. No regressions.

- [ ] **Step 2: Sanity-check DB init standalone**

Run: `python -c "import asyncio, bot.db as d; asyncio.run(d.init_db()); print('init ok')"`
Expected: prints `init ok` (fresh schema applies cleanly).

- [ ] **Step 3: Commit any fixups**

```bash
git add -A
git commit -m "test(agent): phase 0A full-suite verification" --allow-empty
```

---

## Self-Review

**Spec coverage (Phase 0 online-infra items):**
- `ai_suggestions` table with all spec fields → Task 1. ✓
- `ai_suggestion_events` events model → Tasks 1, 4. ✓
- Five independent statuses → Task 1 (columns), Task 4 (denormalization). ✓
- `idempotency_key` with `prompt_version` (refinement 1) → Task 2. ✓
- Event→`human_label` by full chain (refinement 2) → Tasks 2, 4. ✓
- `effective_label = human_label ?? judge_label` → Task 4 (`_recompute_labels`). ✓
- Freshness check → Task 7. ✓
- Feature flags + kill switch → Task 5. ✓
- Two-level safety, decision by code → Task 6. ✓
- `client_id` scoping rule (refinement 3) → Task 1 column + Global Constraints note; transcript table deferred to Phase 4. ✓
- Non-fatal tracing → Tasks 8, 9 (try/except). ✓
- Golden set + baseline eval → **deferred to Phase 0B plan** (documented above). ✓ (intentional split)

**Placeholder scan:** none — every step has runnable code/commands.

**Type consistency:** `record_suggestion` / `get_suggestion` / `get_open_suggestion_by_topic` / `record_suggestion_event` / `derive_human_label` / `compute_idempotency_key` signatures match across Tasks 2–4, their `db/__init__.py` exports, and their use in Tasks 8–9. `check_freshness` returns the same `"current"|"superseded"` strings used by the `freshness_status` column default `"current"`.
