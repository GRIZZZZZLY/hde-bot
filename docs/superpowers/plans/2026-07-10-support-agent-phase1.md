# Support Agent — Phase 1 (Agentic Pipeline) Implementation Plan — rev.2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-shot suggestion generation with an agentic pipeline — cheap policy pre-check → context (structured budgeting) → evidence retrieval → action choice (ANSWER/ASK/ESCALATE/NO_ACTION) → adaptive generation → single-call self-check **against evidence excerpts** → freshness re-check — behind the `AGENT_ENABLED` flag, with full reproducible tracing and no way to lose an `ai_suggestions` row.

**Architecture (rev.2 changes):** Self-check receives evidence *content* (`used_excerpt`), not labels. `run_agent` takes `topic_id`, records the row itself with the real anchor; `register_feedback_pending` ALWAYS records too — both use the same `idempotency_key` inputs, so `INSERT OR IGNORE` dedupes structurally (no flag-based skip; agent crash → legacy row still lands). Policy pre-check runs BEFORE retrieval. Freshness re-check (0A `check_freshness`) runs before returning, with one regeneration on supersession. Models and prompt-version tag come from config/helpers, self-check uses a new public `call_groq_json` transport (no import of the optimizer's private judge transport).

**Tech Stack:** Python 3, aiohttp/Groq, pytest (`asyncio_mode = auto`).

## Global Constraints

- No new third-party dependencies.
- **Kill switch is real**: `config.agent_enabled=False` (default) → live flow calls unchanged `generate_ticket_summary`; `run_agent` never entered.
- **Same output contract**: `run_agent(...) -> tuple[str, str, str, int] | None` (`suit, client, memo, confidence`), identical to `generate_ticket_summary`; `None` → legacy fallback.
- **No lost tracing, no duplicates**: `register_feedback_pending` always calls `record_suggestion` (no flag check). `run_agent` records with the same key inputs — `ticket_id`, `trigger_source="first"`, identical `context_until_post_id` (max post_id of the same posts list), `pipeline_version=config.agent_pipeline_version`, `prompt_version=prompt_version_tag()`. `INSERT OR IGNORE` on `idempotency_key` guarantees exactly one row whichever path wins.
- **Order**: policy pre-check on (title + last client text) BEFORE ContextBuilder/retrieval; ESCALATE short-circuits with zero retrieval/LLM cost.
- **Evidence, not labels**: ContextBuilder returns `evidence: list[dict]` with `{source_type, source_id, rank, score, title, used_excerpt}`; self-check consumes `used_excerpt`s; `retrieved_refs` stores this structure as JSON. (`content_hash` per ref is omitted: `store.find_similar` returns truncated `KnowledgeItem`s without it — documented limitation, revisit in 2B.)
- **Self-check contract**: one LLM call; `status != "supported"` (i.e. both `partially_supported` and `unsupported`) → switch to `fallback_action` + `fallback_client_text`, and set `confidence = 30` (never keep the draft's confidence after a fallback).
- **Freshness**: before returning, re-check the newest post id vs the anchor (0A `check_freshness` logic via an injectable posts fetch). Superseded → refetch posts and regenerate ONCE; superseded again → return the result with an explicit warning line in the memo.
- **Transport/models**: new public `call_groq_json(system, user, *, model, temperature=0.0, max_tokens=600) -> str | None` in `bot/ai_summary.py` (reuses the file's aiohttp+LLM_SEMAPHORE pattern). Draft model `config.agent_draft_model` (env `AGENT_DRAFT_MODEL`, default `llama-3.3-70b-versatile`); self-check model `config.agent_selfcheck_model` (env `AGENT_SELFCHECK_MODEL`, default `openai/gpt-oss-120b`). Runtime NEVER imports `bot.optimizer.*`.
- `prompt_version_tag() -> str` helper in `bot/ai_summary.py`: `"db-active"` when a DB prompt is active, else `"legacy"` — used by BOTH `run_agent` and `register_feedback_pending`.
- Reuse ai_summary collectors (`_build_history_text`, `_detect_equipment`, `_build_system_prompt`, `get_active_format_instructions`); retrieval uses `embed_text` + `store.find_similar` directly (to get ids/scores) with the same `RAG_MIN_SCORE` threshold as `get_rag_context`.
- Action set: `AGENT_ACTIONS = ("ANSWER", "ASK", "ESCALATE", "NO_ACTION")` defined in `bot/agent/actions.py` (no runtime coupling to `bot/optimizer/golden.py`).
- All LLM/IO injectable via keyword-only `_*_fn` params; tests never hit the network.
- Tests: `asyncio_mode = auto`, files `tests/test_agent_*.py`.
- Style: `from __future__ import annotations`, RU docstrings/comments.

## Out of scope

Dynamic few-shot from dialogue_pairs (Phase 2B; `agent_dynamic_fewshot_enabled` stays a no-op), transcript-scoped retrieval (Phase 4), nightly judge (5B), multi-turn button (Phase 3), per-claim partial-support handling (2B; rev.2 maps `partially_supported` to fallback).

## File Structure

- Modify: `bot/config.py` — `agent_draft_model`, `agent_selfcheck_model`.
- Modify: `bot/ai_summary.py` — add `call_groq_json`, `prompt_version_tag` (both additive).
- Create: `bot/agent/actions.py` — `AGENT_ACTIONS`, `build_action_instruction`, `parse_agent_draft`, `extract_client_text`, `compose_memo`.
- Create: `bot/agent/context.py` — `build_history_budgeted`, `build_agent_context` (evidence-structured).
- Create: `bot/agent/generate.py` — `generate_agent_draft`.
- Create: `bot/agent/selfcheck.py` — `self_check` (evidence excerpts in, config model).
- Create: `bot/agent/pipeline.py` — `run_agent` (pre-check→context→draft→post-safety→self-check→freshness→record).
- Modify: `bot/db/suggestion_store.py` — extend `record_suggestion` with agent/trace fields.
- Modify: `bot/handlers/ai_feedback.py::register_feedback_pending` — pass anchor + prompt tag (record stays unconditional).
- Modify: `bot/topic_history.py::_post_ticket_history` — pass `topic_id`/anchor into generation and registration.
- Modify: `bot/topic_manager.py::_generate_summary_with_retry` — flag-gated agent branch with legacy fallback.
- Test: `tests/test_agent_actions.py`, `tests/test_agent_context.py`, `tests/test_agent_generate.py`, `tests/test_agent_selfcheck.py`, `tests/test_agent_pipeline.py`, `tests/test_agent_integration.py`.

---

## Task 1: Config models + `call_groq_json` + `prompt_version_tag`

**Files:**
- Modify: `bot/config.py`
- Modify: `bot/ai_summary.py`
- Test: `tests/test_agent_actions.py` (config asserts) + `tests/test_llm_helper.py` pattern reuse in `tests/test_agent_generate.py` later

**Interfaces:**
- Produces:
  - `config.agent_draft_model: str` (env `AGENT_DRAFT_MODEL`, default `"llama-3.3-70b-versatile"`), `config.agent_selfcheck_model: str` (env `AGENT_SELFCHECK_MODEL`, default `"openai/gpt-oss-120b"`).
  - `bot.ai_summary.call_groq_json(system: str, user: str, *, model: str, temperature: float = 0.0, max_tokens: int = 600) -> str | None` — Groq chat call with `response_format={"type":"json_object"}`, wrapped in `LLM_SEMAPHORE` + `shared_session()`, returns raw content or None on error (mirrors `call_groq_text`'s error handling; place it right after `call_groq_text`).
  - `bot.ai_summary.prompt_version_tag() -> str` — `"db-active"` if `_active_prompt_loaded and _active_format_instructions is not None` else `"legacy"` (sync read of the existing module cache; place next to `get_active_format_instructions`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_actions.py
import bot.config as config_module


def test_agent_model_config_defaults():
    fresh = config_module.Config.from_env()
    assert fresh.agent_draft_model == "llama-3.3-70b-versatile"
    assert fresh.agent_selfcheck_model == "openai/gpt-oss-120b"


def test_agent_model_config_env(monkeypatch):
    monkeypatch.setenv("AGENT_DRAFT_MODEL", "m1")
    monkeypatch.setenv("AGENT_SELFCHECK_MODEL", "m2")
    fresh = config_module.Config.from_env()
    assert fresh.agent_draft_model == "m1"
    assert fresh.agent_selfcheck_model == "m2"


def test_prompt_version_tag_legacy_by_default():
    import bot.ai_summary as ai
    ai._active_prompt_loaded = False
    ai._active_format_instructions = None
    assert ai.prompt_version_tag() == "legacy"
    ai._active_prompt_loaded = True
    ai._active_format_instructions = "custom"
    assert ai.prompt_version_tag() == "db-active"
    ai._active_prompt_loaded = False
    ai._active_format_instructions = None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_actions.py -v`
Expected: FAIL — missing config attrs / `prompt_version_tag`.

- [ ] **Step 3: Implement**

`bot/config.py` — dataclass fields after `agent_staff_user_ids` (or after `agent_call_fixation_enabled` if 2A hasn't landed yet — append at the end of the agent block):

```python
    agent_draft_model: str
    agent_selfcheck_model: str
```

`from_env` additions:

```python
            agent_draft_model=os.getenv(
                "AGENT_DRAFT_MODEL", "llama-3.3-70b-versatile"
            ).strip() or "llama-3.3-70b-versatile",
            agent_selfcheck_model=os.getenv(
                "AGENT_SELFCHECK_MODEL", "openai/gpt-oss-120b"
            ).strip() or "openai/gpt-oss-120b",
```

`bot/ai_summary.py` — after `call_groq_text` (~:120):

```python
async def call_groq_json(
    system: str,
    user: str,
    *,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 600,
) -> str | None:
    """Публичный JSON-вызов Groq для runtime-агента (self-check и т.п.)."""
    if not config.groq_api_key:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {config.groq_api_key}"}
    try:
        async with LLM_SEMAPHORE:
            async with shared_session() as session:
                async with session.post(
                    _GROQ_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("call_groq_json: HTTP %s", resp.status)
                        return None
                    data = await resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("call_groq_json failed: %s", exc)
        return None
```

(Match the file's actual import/name for the timeout and session helpers — copy the shape used by `call_groq_text` in that file verbatim.)

After `get_active_format_instructions` (~:350):

```python
def prompt_version_tag() -> str:
    """Метка версии промпта для трассировки ai_suggestions.
    Согласована между run_agent и register_feedback_pending (общий idempotency_key)."""
    if _active_prompt_loaded and _active_format_instructions is not None:
        return "db-active"
    return "legacy"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_actions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/config.py bot/ai_summary.py tests/test_agent_actions.py
git commit -m "feat(agent): config models, public call_groq_json, prompt_version_tag"
```

---

## Task 2: Pure action helpers

**Files:**
- Create: `bot/agent/actions.py`
- Test: `tests/test_agent_actions.py` (append)

**Interfaces:**
- Produces:
  - `AGENT_ACTIONS = ("ANSWER", "ASK", "ESCALATE", "NO_ACTION")`
  - `build_action_instruction() -> str`
  - `parse_agent_draft(raw: str) -> dict | None` — keys `action, suit, client, memo, confidence (int 0..100, default 50), confidence_reason (str, default "")`; None on malformed / bad action.
  - `extract_client_text(posts, client_id) -> str`
  - `compose_memo(base_memo, *, grounds: list[str], confidence, missing, self_check_status, action, stale_warning=False) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_actions.py (append)
from types import SimpleNamespace

from bot.agent.actions import (
    AGENT_ACTIONS,
    build_action_instruction,
    compose_memo,
    extract_client_text,
    parse_agent_draft,
)


def test_parse_agent_draft_ok_with_fences_and_reason():
    raw = ('```json\n{"action":"ASK","suit":"с","client":"какая модель?",'
           '"memo":"м","confidence":70,"confidence_reason":"нет модели кассы"}\n```')
    d = parse_agent_draft(raw)
    assert d["action"] == "ASK"
    assert d["confidence"] == 70
    assert d["confidence_reason"] == "нет модели кассы"


def test_parse_agent_draft_rejects_bad():
    assert parse_agent_draft('{"action":"MAYBE","suit":"s","client":"c","memo":"m"}') is None
    assert parse_agent_draft("не json") is None
    assert parse_agent_draft("") is None


def test_parse_agent_draft_defaults():
    d = parse_agent_draft('{"action":"ANSWER","suit":"s","client":"c","memo":"m"}')
    assert d["confidence"] == 50
    assert d["confidence_reason"] == ""


def test_build_action_instruction_lists_actions():
    instr = build_action_instruction()
    for a in AGENT_ACTIONS:
        assert a in instr


def test_extract_client_text_by_client_id():
    posts = [
        SimpleNamespace(user_id=1, text="<p>первый</p>"),
        SimpleNamespace(user_id=99, text="ответ оператора"),
        SimpleNamespace(user_id=1, text="<b>второй</b>"),
    ]
    assert extract_client_text(posts, client_id=1) == "второй"
    assert extract_client_text([], client_id=1) == ""


def test_compose_memo_grounds_status_stale():
    memo = compose_memo(
        "перезагрузите кассу", grounds=["KB#12", "wiki:Чеки"], confidence=80,
        missing="модель ОФД", self_check_status="supported", action="ANSWER",
        stale_warning=True,
    )
    assert "KB#12" in memo and "модель ОФД" in memo and "ANSWER" in memo
    assert "нов" in memo.lower()  # предупреждение о новом сообщении клиента
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_actions.py -v`
Expected: FAIL — `No module named 'bot.agent.actions'`.

- [ ] **Step 3: Implement**

```python
# bot/agent/actions.py
"""Чистые помощники агентного пайплайна: инструкция выбора действия,
парсинг ответа модели, извлечение вопроса клиента, сборка Памятки."""
from __future__ import annotations

import html as _html
import json
import re as _re

AGENT_ACTIONS = ("ANSWER", "ASK", "ESCALATE", "NO_ACTION")


def build_action_instruction() -> str:
    return (
        "\n\nВыбери ОДНО действие:\n"
        "- ANSWER — данных достаточно, дай готовое к отправке решение;\n"
        "- ASK — данных не хватает, задай минимальный набор уточняющих вопросов "
        "одним сообщением (не более 4; если вопросы зависят друг от друга — только первый);\n"
        "- ESCALATE — вопрос нельзя решать без оператора (деньги, фискальные "
        "параметры, необратимые действия, доступы);\n"
        "- NO_ACTION — клиент не задал вопрос, ответ не требуется.\n"
        "Верни СТРОГО JSON без пояснений:\n"
        '{"action": "...", "suit": "краткая суть обращения", '
        '"client": "текст для клиента (пусто для NO_ACTION/ESCALATE)", '
        '"memo": "шпаргалка оператору", "confidence": 0-100, '
        '"confidence_reason": "почему такая уверенность"}'
    )


def parse_agent_draft(raw: str) -> dict | None:
    """Парсит JSON-ответ модели (терпим к ```json ограждениям)."""
    if not raw:
        return None
    match = _re.search(r"\{.*\}", raw.strip(), _re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("action") not in AGENT_ACTIONS:
        return None
    for key in ("suit", "client", "memo"):
        if not isinstance(obj.get(key, ""), str):
            return None
    try:
        conf = int(obj.get("confidence", 50))
    except (TypeError, ValueError):
        conf = 50
    return {
        "action": obj["action"],
        "suit": obj.get("suit", "").strip(),
        "client": obj.get("client", "").strip(),
        "memo": obj.get("memo", "").strip(),
        "confidence": max(0, min(100, conf)),
        "confidence_reason": str(obj.get("confidence_reason", "")).strip(),
    }


def _strip_html(text: str) -> str:
    cleaned = _re.sub(r"<[^>]+>", " ", text or "")
    cleaned = _html.unescape(cleaned)
    return _re.sub(r"\s+", " ", cleaned).strip()


def extract_client_text(posts, client_id) -> str:
    """Текст последнего сообщения клиента (по client_id), без HTML."""
    for post in reversed(list(posts)):
        if str(getattr(post, "user_id", "")) == str(client_id):
            text = _strip_html(getattr(post, "text", ""))
            if text:
                return text
    return ""


def compose_memo(
    base_memo: str,
    *,
    grounds: list[str],
    confidence: int,
    missing: str,
    self_check_status: str,
    action: str,
    stale_warning: bool = False,
) -> str:
    """Памятка оператору + основания: источники, уверенность, чего не хватает."""
    lines = [base_memo.strip()] if base_memo.strip() else []
    lines.append(
        f"Действие: {action} · уверенность {confidence}% · self-check: {self_check_status}"
    )
    if grounds:
        lines.append("Основания: " + "; ".join(grounds))
    if missing:
        lines.append("Не хватает: " + missing)
    if stale_warning:
        lines.append("⚠️ Пока готовился ответ, клиент прислал новое сообщение — проверь актуальность.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_actions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/actions.py tests/test_agent_actions.py
git commit -m "feat(agent): pure action helpers"
```

---

## Task 3: ContextBuilder — structured history budget + evidence retrieval

**Files:**
- Create: `bot/agent/context.py`
- Test: `tests/test_agent_context.py`

**Interfaces:**
- Consumes (defaults injectable): `_history_fn=_build_history_text`, `_embed_fn=embed_text`, `_similar_fn=store.find_similar`, `_equipment_fn=_detect_equipment`, `_wiki_fn=get_wiki_context`, `_pattern_fn=find_solution_pattern`.
- Produces:
  - `build_history_budgeted(posts, info, *, budget=3000, _history_fn=None) -> str` — full history if it fits; else first client post's rendering + `"[...пропущено N сообщений...]"` + rendering of the longest tail of WHOLE posts that fits the budget (no mid-message cuts).
  - `build_agent_context(posts, info, ticket_title, company_id="", *, _history_fn=None, _embed_fn=None, _similar_fn=None, _equipment_fn=None, _wiki_fn=None, _pattern_fn=None) -> dict` with keys: `history, client_text, equipment, evidence (list[dict]), retrieval_query, wiki, solution_steps, grounds (labels derived from evidence), confidence`.
  - Evidence item: `{"source_type": "knowledge_item"|"wiki"|"solution_pattern", "source_id", "rank", "score", "title", "used_excerpt"}`. KB items filtered by the same threshold as `get_rag_context` (`RAG_MIN_SCORE` imported from `bot.knowledge.indexer`). `used_excerpt` = item content truncated to 600 chars.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_context.py
from types import SimpleNamespace

from bot.agent.context import build_agent_context, build_history_budgeted


def _mk_posts():
    return [
        SimpleNamespace(user_id=1, text="касса не печатает чек", post_id=1),
        SimpleNamespace(user_id=99, text="проверьте бумагу", post_id=2),
        SimpleNamespace(user_id=1, text="бумага есть", post_id=3),
    ]


def test_history_budgeted_keeps_whole_messages():
    posts = _mk_posts()
    info = SimpleNamespace(client_id=1)

    def hist(p, i):
        return "\n".join(f"Клиент: {x.text}" if x.user_id == 1 else f"Сотрудник: {x.text}"
                         for x in p)

    full = build_history_budgeted(posts, info, budget=10_000, _history_fn=hist)
    assert "касса не печатает" in full and "бумага есть" in full

    # маленький бюджет: первый вопрос + маркер пропуска + хвост целыми сообщениями
    small = build_history_budgeted(posts, info, budget=90, _history_fn=hist)
    assert "касса не печатает" in small            # исходный вопрос сохранён
    assert "пропущено" in small
    assert "бумага есть" in small                  # последнее сообщение целиком


async def test_build_agent_context_evidence_structure():
    posts = _mk_posts()
    info = SimpleNamespace(client_id=1)

    async def fake_embed(text, task_type="query"):
        import numpy as np
        return np.ones(4, dtype=np.float32)

    async def fake_similar(emb, *, limit=3, query_text="", company_id=""):
        item = SimpleNamespace(id=12, content="Меняли бумагу — помогла перезагрузка", quality="good")
        return [(item, 0.91)]

    async def fake_wiki(title):
        return "Статья про чеки"

    async def fake_pattern(equipment, keywords):
        return {"id": 3, "steps": "1. Проверить бумагу"}

    ctx = await build_agent_context(
        posts, info, "Не печатает чек", company_id="c1",
        _history_fn=lambda p, i: "H", _embed_fn=fake_embed, _similar_fn=fake_similar,
        _equipment_fn=lambda t, h: "АТОЛ", _wiki_fn=fake_wiki, _pattern_fn=fake_pattern,
    )
    kb = [e for e in ctx["evidence"] if e["source_type"] == "knowledge_item"][0]
    assert kb["source_id"] == 12 and kb["score"] == 0.91
    assert "перезагрузка" in kb["used_excerpt"]           # контент, не метка
    wiki = [e for e in ctx["evidence"] if e["source_type"] == "wiki"][0]
    assert "Статья" in wiki["used_excerpt"]
    assert ctx["retrieval_query"].startswith("Не печатает чек")
    assert ctx["client_text"] == "бумага есть"
    assert any(g.startswith("KB#") for g in ctx["grounds"])


async def test_build_agent_context_low_score_filtered():
    posts = _mk_posts()
    info = SimpleNamespace(client_id=1)

    async def fake_embed(text, task_type="query"):
        import numpy as np
        return np.ones(4, dtype=np.float32)

    async def low_similar(emb, *, limit=3, query_text="", company_id=""):
        item = SimpleNamespace(id=5, content="слабое совпадение", quality="good")
        return [(item, 0.10)]                              # ниже RAG_MIN_SCORE

    async def none_wiki(title):
        return None

    async def none_pattern(equipment, keywords):
        return None

    ctx = await build_agent_context(
        posts, info, "t",
        _history_fn=lambda p, i: "H", _embed_fn=fake_embed, _similar_fn=low_similar,
        _equipment_fn=lambda t, h: None, _wiki_fn=none_wiki, _pattern_fn=none_pattern,
    )
    assert ctx["evidence"] == []
    assert ctx["grounds"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_context.py -v`
Expected: FAIL — `No module named 'bot.agent.context'`.

- [ ] **Step 3: Implement**

```python
# bot/agent/context.py
"""Сборка контекста агента: структурный бюджет истории (без разрезов посередине
сообщения) и evidence-retrieval со score/id/фрагментами для self-check и трассировки."""
from __future__ import annotations

from .actions import extract_client_text

_EXCERPT_LIMIT = 600


def build_history_budgeted(posts, info, *, budget: int = 3000, _history_fn=None) -> str:
    if _history_fn is None:
        from ..ai_summary import _build_history_text as _history_fn
    posts = list(posts)
    full = _history_fn(posts, info)
    if len(full) <= budget or len(posts) <= 2:
        return full
    client_id = getattr(info, "client_id", "")
    first_idx = next(
        (i for i, p in enumerate(posts) if str(p.user_id) == str(client_id)), 0
    )
    head = _history_fn([posts[first_idx]], info)
    # хвост целыми сообщениями, сколько влезает в остаток бюджета
    remaining = budget - len(head)
    tail_posts: list = []
    for post in reversed(posts[first_idx + 1:]):
        candidate = _history_fn([post] + [p for p in tail_posts], info)
        if len(candidate) > remaining:
            break
        tail_posts.insert(0, post)
    skipped = len(posts) - 1 - len(tail_posts) - first_idx
    marker = f"\n[...пропущено {max(skipped, 0)} сообщений...]\n" if skipped > 0 else "\n"
    tail = _history_fn(tail_posts, info) if tail_posts else ""
    return head + marker + tail


async def build_agent_context(
    posts,
    info,
    ticket_title: str,
    company_id: str = "",
    *,
    _history_fn=None,
    _embed_fn=None,
    _similar_fn=None,
    _equipment_fn=None,
    _wiki_fn=None,
    _pattern_fn=None,
) -> dict:
    if _embed_fn is None:
        from ..knowledge.indexer import embed_text as _embed_fn
    if _similar_fn is None:
        from ..knowledge.store import find_similar as _similar_fn
    if _equipment_fn is None:
        from ..ai_summary import _detect_equipment as _equipment_fn
    if _wiki_fn is None:
        from ..wiki.searcher import get_wiki_context as _wiki_fn
    if _pattern_fn is None:
        from ..db import find_solution_pattern as _pattern_fn
    from ..knowledge.indexer import RAG_MIN_SCORE

    history = build_history_budgeted(posts, info, _history_fn=_history_fn)
    client_text = extract_client_text(posts, getattr(info, "client_id", ""))
    equipment = _equipment_fn(ticket_title, history)
    retrieval_query = f"{ticket_title}\n{client_text}"[:600]

    evidence: list[dict] = []
    confidence = 0
    embedding = await _embed_fn(retrieval_query, task_type="query")
    if embedding is not None:
        results = await _similar_fn(
            embedding, limit=3, query_text=retrieval_query, company_id=company_id
        )
        rank = 0
        for item, score in results:
            if score < RAG_MIN_SCORE:
                continue
            rank += 1
            evidence.append({
                "source_type": "knowledge_item",
                "source_id": item.id,
                "rank": rank,
                "score": round(float(score), 4),
                "title": getattr(item, "title", None),
                "used_excerpt": (item.content or "")[:_EXCERPT_LIMIT],
            })
            confidence = max(confidence, int(round(float(score) * 100)))

    wiki = await _wiki_fn(ticket_title)
    if wiki:
        evidence.append({
            "source_type": "wiki", "source_id": None,
            "rank": len(evidence) + 1, "score": None,
            "title": ticket_title[:80], "used_excerpt": wiki[:_EXCERPT_LIMIT],
        })
    pattern = await _pattern_fn(equipment, ticket_title)
    solution_steps = pattern.get("steps") if pattern else None
    if solution_steps:
        evidence.append({
            "source_type": "solution_pattern", "source_id": pattern.get("id"),
            "rank": len(evidence) + 1, "score": None,
            "title": equipment or "", "used_excerpt": solution_steps[:_EXCERPT_LIMIT],
        })

    grounds = []
    kb_n = 0
    for e in evidence:
        if e["source_type"] == "knowledge_item":
            kb_n += 1
            grounds.append(f"KB#{e['source_id']}")
        elif e["source_type"] == "wiki":
            grounds.append(f"wiki:{(e['title'] or '')[:40]}")
        else:
            grounds.append(f"pattern:{e['title'] or '?'}")

    return {
        "history": history,
        "client_text": client_text,
        "equipment": equipment,
        "evidence": evidence,
        "retrieval_query": retrieval_query,
        "wiki": wiki,
        "solution_steps": solution_steps,
        "grounds": grounds,
        "confidence": confidence,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_context.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/context.py tests/test_agent_context.py
git commit -m "feat(agent): structured history budget + evidence retrieval"
```

---

## Task 4: Draft generation (config model)

**Files:**
- Create: `bot/agent/generate.py`
- Test: `tests/test_agent_generate.py`

**Interfaces:**
- Consumes: `_build_system_prompt`, `get_active_format_instructions`, `call_groq_text` (draft is free-form JSON-in-text; `parse_agent_draft` handles fences), `config.agent_draft_model`.
- Produces: `generate_agent_draft(context, ticket_title, *, _call_fn=None, _prompt_fn=None, _format_fn=None) -> dict | None`. rag_examples for the base prompt are derived from evidence excerpts (`knowledge_item` entries), so the draft sees the same evidence the self-check will verify against.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_generate.py
from bot.agent.generate import generate_agent_draft

_CTX = {
    "history": "Клиент: касса не печатает чек",
    "client_text": "касса не печатает чек",
    "equipment": "АТОЛ",
    "evidence": [
        {"source_type": "knowledge_item", "source_id": 12, "rank": 1,
         "score": 0.9, "title": None, "used_excerpt": "Помогла перезагрузка"},
    ],
    "retrieval_query": "q", "wiki": None, "solution_steps": None,
    "grounds": ["KB#12"], "confidence": 90,
}


async def test_generate_agent_draft_passes_evidence_and_parses():
    seen = {}

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None, temperature=None):
        seen["system"] = system
        seen["model"] = model
        return '{"action":"ANSWER","suit":"чек","client":"перезагрузите кассу","memo":"м","confidence":85}'

    def fake_prompt(title, rag_examples=None, wiki_context=None, *, equipment=None,
                    solution_steps=None, format_instructions=None):
        seen["rag"] = rag_examples
        return "SYSTEM"

    async def fake_format():
        return "FORMAT"

    draft = await generate_agent_draft(
        _CTX, "Не печатает чек",
        _call_fn=fake_call, _prompt_fn=fake_prompt, _format_fn=fake_format,
    )
    assert draft["action"] == "ANSWER"
    assert seen["rag"] == ["Помогла перезагрузка"]     # evidence → draft prompt
    assert "JSON" in seen["system"]
    from bot.config import config
    assert seen["model"] == config.agent_draft_model


async def test_generate_agent_draft_none_on_garbage():
    async def bad_call(prompt, *, system=None, model=None, max_tokens=None, temperature=None):
        return "мусор"

    async def fake_format():
        return "F"

    assert await generate_agent_draft(
        _CTX, "t", _call_fn=bad_call, _prompt_fn=lambda *a, **k: "S", _format_fn=fake_format,
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_generate.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# bot/agent/generate.py
"""Черновая генерация агента: базовый system-промпт ai_summary + инструкция
выбора действия; модель — из config.agent_draft_model."""
from __future__ import annotations

from .actions import build_action_instruction, parse_agent_draft


async def generate_agent_draft(
    context: dict,
    ticket_title: str,
    *,
    _call_fn=None,
    _prompt_fn=None,
    _format_fn=None,
) -> dict | None:
    if _call_fn is None:
        from ..ai_summary import call_groq_text as _call_fn
    if _prompt_fn is None:
        from ..ai_summary import _build_system_prompt as _prompt_fn
    if _format_fn is None:
        from ..ai_summary import get_active_format_instructions as _format_fn
    from ..config import config

    rag_examples = [
        e["used_excerpt"] for e in context.get("evidence", [])
        if e["source_type"] == "knowledge_item"
    ] or None
    format_instructions = await _format_fn()
    base_system = _prompt_fn(
        ticket_title,
        rag_examples=rag_examples,
        wiki_context=context.get("wiki"),
        equipment=context.get("equipment"),
        solution_steps=context.get("solution_steps"),
        format_instructions=format_instructions,
    )
    system = base_system + build_action_instruction()
    raw = await _call_fn(
        context["history"], system=system, model=config.agent_draft_model,
        max_tokens=800, temperature=0.3,
    )
    return parse_agent_draft(raw or "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_generate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/generate.py tests/test_agent_generate.py
git commit -m "feat(agent): draft generation with evidence-fed prompt, config model"
```

---

## Task 5: Self-check against evidence excerpts

**Files:**
- Create: `bot/agent/selfcheck.py`
- Test: `tests/test_agent_selfcheck.py`

**Interfaces:**
- Consumes: `bot.ai_summary.call_groq_json` (default transport), `config.agent_selfcheck_model`.
- Produces: `self_check(client_text, generated_client, evidence, history, *, _call_fn=None) -> dict` — the prompt includes each evidence item's `used_excerpt` (numbered); returns `{"status", "fallback_action", "fallback_client_text"}`; conservative default `unsupported/ASK` on any failure. NOTE: verification is against evidence CONTENT — this is the rev.2 blocker fix.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_selfcheck.py
import json

from bot.agent.selfcheck import self_check

_EVIDENCE = [
    {"source_type": "knowledge_item", "source_id": 12, "rank": 1, "score": 0.9,
     "title": None, "used_excerpt": "Клиенту помогла перезагрузка кассы после замены бумаги"},
]


async def test_self_check_prompt_contains_evidence_content():
    seen = {}

    async def fake_call(system, user, *, model=None, temperature=0.0, max_tokens=600):
        seen["user"] = user
        seen["model"] = model
        return json.dumps({"status": "supported", "fallback_action": "ASK",
                           "fallback_client_text": ""})

    r = await self_check("вопрос", "перезагрузите кассу", _EVIDENCE, "история",
                         _call_fn=fake_call)
    assert r["status"] == "supported"
    assert "помогла перезагрузка кассы" in seen["user"]   # КОНТЕНТ, не метка
    from bot.config import config
    assert seen["model"] == config.agent_selfcheck_model


async def test_self_check_unsupported_fallback():
    async def fake_call(system, user, *, model=None, temperature=0.0, max_tokens=600):
        return json.dumps({"status": "unsupported", "fallback_action": "ESCALATE",
                           "fallback_client_text": ""})

    r = await self_check("в", "выдумка", [], "и", _call_fn=fake_call)
    assert r["status"] == "unsupported"
    assert r["fallback_action"] == "ESCALATE"


async def test_self_check_conservative_default():
    async def bad_call(system, user, *, model=None, temperature=0.0, max_tokens=600):
        return "не json"

    r = await self_check("в", "о", [], "и", _call_fn=bad_call)
    assert r == {"status": "unsupported", "fallback_action": "ASK",
                 "fallback_client_text": ""}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_selfcheck.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# bot/agent/selfcheck.py
"""Self-check одним LLM-вызовом ПРОТИВ СОДЕРЖИМОГО источников (used_excerpt),
а не их названий. Консервативный дефолт unsupported→ASK, никогда не падает."""
from __future__ import annotations

import json

_DEFAULT = {"status": "unsupported", "fallback_action": "ASK", "fallback_client_text": ""}
_STATUSES = ("supported", "partially_supported", "unsupported")


def _render_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return "Источники не найдены."
    lines = []
    for i, e in enumerate(evidence, 1):
        label = e.get("source_type", "?")
        lines.append(f"[{i}] ({label}) {e.get('used_excerpt', '')}")
    return "\n".join(lines)


def _build_selfcheck_prompt(client_text, generated_client, evidence, history):
    system = (
        "Ты проверяешь ответ техподдержки кассового ПО на опору в источниках. "
        "Каждое фактическое утверждение ответа должно подтверждаться историей "
        "тикета ИЛИ приведёнными фрагментами источников. Выдуманные шаги, модели, "
        "настройки — нарушение.\n"
        "status: supported — все утверждения подтверждены; partially_supported — "
        "часть без опоры; unsupported — ключевые утверждения не подтверждены.\n"
        "Если status != supported, предложи безопасный fallback: ASK (уточнить у "
        "клиента) или ESCALATE (передать оператору) и текст fallback_client_text.\n"
        'Верни СТРОГО JSON: {"status":"...","fallback_action":"ASK|ESCALATE",'
        '"fallback_client_text":"..."}'
    )
    user = (
        f"Вопрос клиента: {client_text}\n\n"
        f"История:\n{history}\n\n"
        f"Фрагменты источников:\n{_render_evidence(evidence)}\n\n"
        f"Проверяемый ответ клиенту:\n{generated_client}"
    )
    return system, user


async def self_check(
    client_text: str,
    generated_client: str,
    evidence: list[dict],
    history: str,
    *,
    _call_fn=None,
) -> dict:
    if _call_fn is None:
        from ..ai_summary import call_groq_json as _call_fn
    from ..config import config

    system, user = _build_selfcheck_prompt(client_text, generated_client, evidence, history)
    try:
        raw = await _call_fn(system, user, model=config.agent_selfcheck_model)
        obj = json.loads(raw)
    except Exception:
        return dict(_DEFAULT)
    if not isinstance(obj, dict) or obj.get("status") not in _STATUSES:
        return dict(_DEFAULT)
    action = obj.get("fallback_action")
    if action not in ("ASK", "ESCALATE"):
        action = "ASK"
    return {
        "status": obj["status"],
        "fallback_action": action,
        "fallback_client_text": str(obj.get("fallback_client_text", "")),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_selfcheck.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/selfcheck.py tests/test_agent_selfcheck.py
git commit -m "feat(agent): self-check verifies against evidence excerpts"
```

---

## Task 6: Extend `record_suggestion` with full trace fields

**Files:**
- Modify: `bot/db/suggestion_store.py`
- Test: `tests/test_suggestion_store.py` (append)

**Interfaces:**
- Produces: `record_suggestion` gains keyword-only optional params: `action_type=None`, `self_check=None`, `retrieved_refs=None`, `confidence=None`, `confidence_reason=None`, `retrieval_query=None`, `retrieval_config_version=None`, `embedding_model=None`, `generation_ms=None`. All columns already exist (Phase 0A schema). Backward compatible.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_suggestion_store.py (append)
async def test_record_suggestion_stores_full_trace():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="TA", topic_id=9, trigger_source="first",
        context_until_post_id="50", pipeline_version="v1", prompt_version="legacy",
        model="llama-3.3-70b-versatile",
        action_type="ASK", self_check='{"status":"unsupported"}',
        retrieved_refs='[{"source_type":"knowledge_item","source_id":12,"score":0.9}]',
        confidence=30, confidence_reason="fallback после self-check",
        retrieval_query="Не печатает чек", retrieval_config_version="v1",
        embedding_model="intfloat/multilingual-e5-large", generation_ms=4200,
    )
    row = await get_suggestion(sid)
    assert row["action_type"] == "ASK"
    assert row["retrieval_query"] == "Не печатает чек"
    assert row["embedding_model"] == "intfloat/multilingual-e5-large"
    assert row["generation_ms"] == 4200
    assert row["model"] == "llama-3.3-70b-versatile"
    assert row["confidence"] == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_suggestion_store.py -k full_trace -v`
Expected: FAIL — unexpected keyword argument.

- [ ] **Step 3: Implement**

Extend `record_suggestion`'s signature (after `model: str | None = None`):

```python
    action_type: str | None = None,
    self_check: str | None = None,
    retrieved_refs: str | None = None,
    confidence: int | None = None,
    confidence_reason: str | None = None,
    retrieval_query: str | None = None,
    retrieval_config_version: str | None = None,
    embedding_model: str | None = None,
    generation_ms: int | None = None,
```

Extend the INSERT to include the nine new columns (keep column order and the `?` count in sync):

```python
        await db.execute(
            "INSERT OR IGNORE INTO ai_suggestions "
            "(ticket_id, topic_id, trigger_source, context_until_post_id, client_id, "
            " idempotency_key, title, history, client_text, ai_answer, ai_full_text, "
            " pipeline_version, prompt_version, model, action_type, self_check, "
            " retrieved_refs, confidence, confidence_reason, retrieval_query, "
            " retrieval_config_version, embedding_model, generation_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_id, topic_id, trigger_source, context_until_post_id, client_id,
                key, title, history, client_text, ai_answer, ai_full_text,
                pipeline_version, prompt_version, model, action_type, self_check,
                retrieved_refs, confidence, confidence_reason, retrieval_query,
                retrieval_config_version, embedding_model, generation_ms,
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_suggestion_store.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add bot/db/suggestion_store.py tests/test_suggestion_store.py
git commit -m "feat(agent): record_suggestion full trace fields"
```

---

## Task 7: Pipeline orchestrator `run_agent` (rev.2 order + freshness)

**Files:**
- Create: `bot/agent/pipeline.py`
- Test: `tests/test_agent_pipeline.py`

**Interfaces:**
- Produces: `run_agent(posts, info, *, ticket_title, ticket_id, topic_id=None, company_id="", _context_fn=None, _draft_fn=None, _selfcheck_fn=None, _safety_pre=None, _safety_post=None, _record_fn=None, _posts_fn=None) -> tuple[str, str, str, int] | None`.
- Orchestration (rev.2):
  1. `client_text = extract_client_text(posts, info.client_id)` — cheap, no retrieval.
  2. `pre = _safety_pre(f"{ticket_title}\n{client_text}")` → ESCALATE short-circuits: no ContextBuilder, no retrieval, no LLM.
  3. Else full context → draft; draft None → return None (legacy fallback; a row will still be recorded by `register_feedback_pending`).
  4. Post-safety on `draft["client"]` → force ESCALATE.
  5. `action == ANSWER` → self_check with `context["evidence"]`; `status != "supported"` → switch to fallback action/text, `confidence = 30`.
  6. Freshness: `_posts_fn(ticket_id)` (default fetches via `HDEApiClient.get_ticket_posts`), compare max post_id vs anchor; superseded → refetch posts + ONE full regeneration (loop attempt 2); superseded again → keep result, `stale_warning=True` in memo.
  7. compose memo; record full trace with `topic_id`, `generation_ms`, evidence refs, `prompt_version_tag()`; non-fatal.
  8. Return the tuple.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_pipeline.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# bot/agent/pipeline.py
"""Оркестратор агентного пайплайна (Phase 1, rev.2).

Порядок: дешёвый pre-check → контекст+retrieval → драфт → post-safety →
self-check (evidence) → freshness (одна перегенерация) → Памятка → запись."""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)


def _anchor_post_id(posts) -> str | None:
    ids = [getattr(p, "post_id", None) for p in posts]
    ids = [int(i) for i in ids if i is not None]
    return str(max(ids)) if ids else None


async def _default_posts_fn(ticket_id: str):
    from ..hde_api import HDEApiClient
    return await HDEApiClient().get_ticket_posts(ticket_id)


async def run_agent(
    posts,
    info,
    *,
    ticket_title: str,
    ticket_id: str,
    topic_id: int | None = None,
    company_id: str = "",
    _context_fn=None,
    _draft_fn=None,
    _selfcheck_fn=None,
    _safety_pre=None,
    _safety_post=None,
    _record_fn=None,
    _posts_fn=None,
) -> tuple[str, str, str, int] | None:
    if _context_fn is None:
        from .context import build_agent_context as _context_fn
    if _draft_fn is None:
        from .generate import generate_agent_draft as _draft_fn
    if _selfcheck_fn is None:
        from .selfcheck import self_check as _selfcheck_fn
    if _safety_pre is None:
        from .safety import pre_generation_policy_check as _safety_pre
    if _safety_post is None:
        from .safety import post_generation_safety_check as _safety_post
    if _record_fn is None:
        from ..db import record_suggestion as _record_fn
    if _posts_fn is None:
        _posts_fn = _default_posts_fn

    from .actions import compose_memo, extract_client_text

    started = time.monotonic()
    posts = list(posts)
    client_text_quick = extract_client_text(posts, getattr(info, "client_id", ""))

    # 1-2. Дешёвый policy pre-check ДО retrieval
    pre = _safety_pre(f"{ticket_title}\n{client_text_quick}")
    if pre.action == "ESCALATE":
        memo = compose_memo(
            f"⚠️ Эскалация ({pre.category}): вопрос требует оператора.",
            grounds=[], confidence=0, missing="решение оператора",
            self_check_status="n/a", action="ESCALATE",
        )
        await _record_nonfatal(
            _record_fn, posts=posts, info=info, ticket_id=ticket_id,
            topic_id=topic_id, ticket_title=ticket_title, history="",
            client_text=client_text_quick, client="", suit="", memo=memo,
            action="ESCALATE", self_status="n/a", evidence=[],
            retrieval_query=None, confidence=0,
            confidence_reason=f"policy pre-check: {pre.category}",
            started=started,
        )
        return "", "", memo, 0

    # 3-6. Полный проход; при superseded — одна перегенерация
    stale_warning = False
    context = draft = None
    action = suit = client = base_memo = ""
    confidence = 0
    confidence_reason = ""
    self_status = "n/a"
    for attempt in range(2):
        context = await _context_fn(posts, info, ticket_title, company_id)
        draft = await _draft_fn(context, ticket_title)
        if draft is None:
            return None  # драфт не удался → откат на legacy
        action = draft["action"]
        suit, client, base_memo = draft["suit"], draft["client"], draft["memo"]
        confidence, confidence_reason = draft["confidence"], draft["confidence_reason"]
        self_status = "n/a"

        post_check = _safety_post(client)
        if post_check.action == "ESCALATE":
            action, client, confidence = "ESCALATE", "", 0
            base_memo = (f"⚠️ Эскалация ({post_check.category}): предложенный ответ "
                         f"небезопасен. " + base_memo)
        elif action == "ANSWER":
            check = await _selfcheck_fn(
                context["client_text"], client, context["evidence"], context["history"]
            )
            self_status = check["status"]
            if self_status != "supported":
                action = check["fallback_action"]
                client = check["fallback_client_text"]
                confidence = 30
                confidence_reason = f"fallback после self-check: {self_status}"

        # freshness: не появился ли новый пост, пока генерировали
        anchor = _anchor_post_id(posts)
        try:
            fresh_posts = list(await _posts_fn(ticket_id))
        except Exception as exc:
            logger.warning("run_agent: freshness fetch failed: %s", exc)
            fresh_posts = posts
        fresh_anchor = _anchor_post_id(fresh_posts)
        if fresh_anchor == anchor or attempt == 1:
            stale_warning = fresh_anchor != anchor
            break
        posts = fresh_posts  # superseded → одна перегенерация на свежих постах

    missing = "" if action == "ANSWER" else "нужны уточнения/оператор"
    memo = compose_memo(
        base_memo, grounds=context["grounds"], confidence=confidence,
        missing=missing, self_check_status=self_status, action=action,
        stale_warning=stale_warning,
    )
    await _record_nonfatal(
        _record_fn, posts=posts, info=info, ticket_id=ticket_id, topic_id=topic_id,
        ticket_title=ticket_title, history=context["history"],
        client_text=context["client_text"], client=client, suit=suit, memo=memo,
        action=action, self_status=self_status, evidence=context["evidence"],
        retrieval_query=context["retrieval_query"], confidence=confidence,
        confidence_reason=confidence_reason, started=started,
    )
    return suit, client, memo, confidence


async def _record_nonfatal(
    record_fn, *, posts, info, ticket_id, topic_id, ticket_title, history,
    client_text, client, suit, memo, action, self_status, evidence,
    retrieval_query, confidence, confidence_reason, started,
) -> None:
    try:
        from ..ai_summary import prompt_version_tag
        from ..config import config
        await record_fn(
            ticket_id=ticket_id,
            topic_id=topic_id,
            trigger_source="first",
            context_until_post_id=_anchor_post_id(posts),
            client_id=str(getattr(info, "client_id", "") or "") or None,
            pipeline_version=config.agent_pipeline_version,
            prompt_version=prompt_version_tag(),
            title=ticket_title,
            history=history,
            client_text=client_text,
            ai_answer=client,
            ai_full_text=f"{suit}\n{client}\n{memo}",
            model=config.agent_draft_model,
            action_type=action,
            self_check=json.dumps({"status": self_status}, ensure_ascii=False),
            retrieved_refs=json.dumps(evidence, ensure_ascii=False),
            confidence=confidence,
            confidence_reason=confidence_reason,
            retrieval_query=retrieval_query,
            retrieval_config_version="v1",
            embedding_model="intfloat/multilingual-e5-large",
            generation_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        logger.warning("run_agent: suggestion record failed: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_pipeline.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/pipeline.py tests/test_agent_pipeline.py
git commit -m "feat(agent): run_agent — pre-check first, evidence self-check, freshness"
```

---

## Task 8: Integration — single record owner via shared idempotency key

**Files:**
- Modify: `bot/topic_manager.py` (`_generate_summary_with_retry`)
- Modify: `bot/topic_history.py` (`_post_ticket_history` and `retry_missing_ai_summaries` call sites)
- Modify: `bot/handlers/ai_feedback.py::register_feedback_pending`
- Test: `tests/test_agent_integration.py`

**Interfaces:**
- `_generate_summary_with_retry(posts, info, *, ticket_title="", ticket_id="", company_id="", topic_id=None, attempts=3, pause=30.0)` — new kwarg `topic_id`; agent branch gated by `config.agent_enabled and config.agent_auto_first_suggestion_enabled`; `None`/exception → existing legacy loop unchanged.
- `_post_ticket_history` passes `topic_id=topic_id` into `_generate_summary_with_retry`, and passes `context_until_post_id=str(max post_id of all_posts)` into `register_feedback_pending` (also in `retry_missing_ai_summaries`).
- `register_feedback_pending`: record stays UNCONDITIONAL (rev.2 removes any flag check). Its `record_suggestion` call now uses `prompt_version=prompt_version_tag()` (import from `..ai_summary`) instead of the hardcoded `"legacy"` — this makes its idempotency key identical to the agent's, so `INSERT OR IGNORE` dedupes; when the agent crashed, this call is the safety net that still lands a row.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_integration.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot.config as config_module
import bot.db as db_module
from bot.db.suggestion_store import compute_idempotency_key, get_open_suggestion_by_topic
from bot.handlers.ai_feedback import register_feedback_pending


async def test_register_always_records_with_anchor_and_tag(monkeypatch):
    await db_module.init_db()
    monkeypatch.setattr(config_module.config, "agent_enabled", True)  # флаг не влияет
    await register_feedback_pending(
        topic_id=1, ticket_id="T1", history="h", title="t",
        answer_text="a", ai_full_text="f", context_until_post_id="42",
    )
    row = await get_open_suggestion_by_topic(1)
    assert row is not None
    assert row["context_until_post_id"] == "42"
    assert row["prompt_version"] in ("legacy", "db-active")


async def test_agent_row_and_register_row_dedupe_to_one(monkeypatch):
    """Агент записал строку; register с теми же ключами не создаёт дубль."""
    await db_module.init_db()
    from bot.ai_summary import prompt_version_tag
    from bot.db.suggestion_store import record_suggestion

    kw = dict(ticket_id="T2", trigger_source="first", context_until_post_id="10",
              pipeline_version=config_module.config.agent_pipeline_version,
              prompt_version=prompt_version_tag())
    sid_agent = await record_suggestion(topic_id=5, action_type="ANSWER", **kw)
    await register_feedback_pending(
        topic_id=5, ticket_id="T2", history="h", title="t",
        answer_text="a", ai_full_text="f", context_until_post_id="10",
    )
    row = await get_open_suggestion_by_topic(5)
    assert row["id"] == sid_agent                     # та же строка
    assert row["action_type"] == "ANSWER"             # trace агента не затёрт


async def test_generate_with_retry_agent_branch_and_fallback(monkeypatch):
    import bot.topic_manager as tm
    monkeypatch.setattr(config_module.config, "agent_enabled", True)
    monkeypatch.setattr(config_module.config, "agent_auto_first_suggestion_enabled", True)
    posts = [SimpleNamespace(user_id=1, text="q", post_id=1)]
    info = SimpleNamespace(client_id=1)

    with patch("bot.topic_manager.run_agent", new=AsyncMock(
        return_value=("суть", "клиенту", "памятка", 88)
    )) as ok_agent, patch(
        "bot.topic_manager.generate_ticket_summary", new=AsyncMock()
    ) as old:
        result = await tm._generate_summary_with_retry(
            posts, info, ticket_title="t", ticket_id="T3", topic_id=7,
        )
    assert result == ("суть", "клиенту", "памятка", 88)
    ok_agent.assert_awaited()
    assert ok_agent.await_args.kwargs["topic_id"] == 7
    old.assert_not_awaited()

    # агент упал → легаси путь отработал
    with patch("bot.topic_manager.run_agent", new=AsyncMock(
        side_effect=RuntimeError("boom")
    )), patch("bot.topic_manager.generate_ticket_summary", new=AsyncMock(
        return_value=("с", "к", "п", 50)
    )) as old2:
        result = await tm._generate_summary_with_retry(
            posts, info, ticket_title="t", ticket_id="T4", topic_id=7,
        )
    assert result == ("с", "к", "п", 50)
    old2.assert_awaited()


async def test_agent_enabled_but_auto_first_disabled_uses_legacy(monkeypatch):
    import bot.topic_manager as tm
    monkeypatch.setattr(config_module.config, "agent_enabled", True)
    monkeypatch.setattr(config_module.config, "agent_auto_first_suggestion_enabled", False)
    with patch("bot.topic_manager.run_agent", new=AsyncMock()) as agent, patch(
        "bot.topic_manager.generate_ticket_summary",
        new=AsyncMock(return_value=("с", "к", "п", 50)),
    ):
        result = await tm._generate_summary_with_retry(
            [SimpleNamespace(user_id=1, text="q", post_id=1)],
            SimpleNamespace(client_id=1), ticket_title="t", ticket_id="T5",
        )
    assert result == ("с", "к", "п", 50)
    agent.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_integration.py -v`
Expected: FAIL — missing kwargs / no `run_agent` in topic_manager.

- [ ] **Step 3: Implement**

**(a) `bot/handlers/ai_feedback.py::register_feedback_pending`** — two edits inside the existing non-fatal record block (added in 0A):
- add import inside the try: `from ..ai_summary import prompt_version_tag`;
- change `prompt_version="legacy"` → `prompt_version=prompt_version_tag()`.
No flag checks; the record stays unconditional. (If a prior rev added an `if config.agent_enabled: return` skip — REMOVE it.)

**(b) `bot/topic_history.py`** — in `_post_ticket_history` (and the identical block in `retry_missing_ai_summaries`):
- compute the anchor once after `all_posts` is built: `anchor = str(max((p.post_id for p in all_posts), default="")) or None`;
- pass `topic_id=topic_id` into the `_generate_summary_with_retry(...)` call;
- pass `context_until_post_id=anchor` into `register_feedback_pending(...)`.

**(c) `bot/topic_manager.py`** — module-top import:

```python
from .agent.pipeline import run_agent
```

`_generate_summary_with_retry` gains `topic_id=None` kwarg and the agent branch before the existing loop:

```python
async def _generate_summary_with_retry(
    posts, info, *, ticket_title="", ticket_id="", company_id="",
    topic_id=None, attempts=3, pause=30.0
):
    from .config import config
    if config.agent_enabled and config.agent_auto_first_suggestion_enabled:
        try:
            result = await run_agent(
                posts, info, ticket_title=ticket_title, ticket_id=ticket_id,
                topic_id=topic_id, company_id=company_id,
            )
            if result is not None:
                return result
        except Exception as exc:
            logger.warning("run_agent failed, falling back to summary: %s", exc)
    # ... существующий retry-цикл generate_ticket_summary БЕЗ изменений ...
```

(Use the module's existing `logger`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_integration.py -v`
Expected: PASS.

- [ ] **Step 5: Regression + commit**

Run: `python -m pytest tests/test_topic_manager.py tests/test_ai_feedback_flow.py tests/test_ai_feedback_events.py -v`
Expected: PASS (flags default off → legacy flow byte-identical).

```bash
git add bot/topic_manager.py bot/topic_history.py bot/handlers/ai_feedback.py tests/test_agent_integration.py
git commit -m "feat(agent): wire run_agent; single record owner via shared idempotency key"
```

---

## Task 9: Full-suite verification

- [ ] **Step 1:** `python -m pytest -q` — PASS, no regressions.
- [ ] **Step 2:** `python -c "import bot.main" 2>&1 | tail -1` — clean import (no cycles from topic_manager→agent.pipeline).
- [ ] **Step 3:** Default-off check: `python -c "import bot.config as c; print(c.config.agent_enabled)"` → `False`.
- [ ] **Step 4:** `git commit -m "test(agent): phase 1 verification" --allow-empty`.

---

## Self-Review (rev.2)

**Review findings addressed:**
1. Self-check sees evidence CONTENT (`used_excerpt`) → Tasks 3, 5, 7. ✓
2. Single record owner: both paths record with identical key inputs; INSERT OR IGNORE dedupes; agent crash → register still lands a row; `topic_id` real → Tasks 7, 8; test `test_agent_row_and_register_row_dedupe_to_one`. ✓
3. Freshness in Phase 1: re-check + one regeneration + stale memo warning → Task 7 step 6. ✓
4. Pre-check BEFORE retrieval, on title+client_text → Task 7 steps 1–2; test asserts ContextBuilder not called. ✓
5. Structured `retrieved_refs` + retrieval_query/config_version/embedding_model/model/confidence_reason/prompt tag recorded → Tasks 3, 6, 7. (`content_hash` per ref omitted — `find_similar` doesn't return it; documented in Global Constraints.) ✓
6. No optimizer import in runtime: public `call_groq_json` + config models → Tasks 1, 5. ✓
7. `partially_supported` → fallback, confidence reset to 30 → Task 7; test. ✓
8. Structured history budgeting (whole messages, first question kept, gap marker) → Task 3 `build_history_budgeted`. ✓
9. Added tests: ASK/NO_ACTION, partially_supported, pre-policy ESCALATE, post-safety ESCALATE, superseded regen, record-failure non-fatal, agent-crash→legacy, auto-first-disabled, real topic_id, dedupe. ✓

**Placeholder scan:** clean; Task 8 references existing code blocks by their 0A-era content, with explicit edit instructions.

**Type consistency:** `run_agent` tuple contract preserved; context dict keys consumed by generate/selfcheck/pipeline match Task 3's return; `record_suggestion` extended kwargs (Task 6) match `_record_nonfatal`'s call (Task 7) and register's call (Task 8); `compute_idempotency_key` inputs align between Task 7 (`_record_nonfatal`) and Task 8 (register) — same ticket_id/trigger_source/anchor/pipeline_version/prompt tag.
