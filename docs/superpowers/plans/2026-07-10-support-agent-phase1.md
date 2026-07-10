# Support Agent — Phase 1 (Agentic Pipeline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-shot suggestion generation with an agentic pipeline — policy pre-check → context → retrieval → action choice (ANSWER/ASK/ESCALATE/NO_ACTION) → adaptive generation → single-call self-check → mapped output — behind the `AGENT_ENABLED` flag, so the operator gets a genuinely useful first suggestion (spec priority #1) while the old path stays a one-flag fallback.

**Architecture:** New `bot/agent/` modules — `actions.py` (pure parse/compose), `context.py` (ContextBuilder reusing `bot/ai_summary.py` collectors), `generate.py` (draft LLM call), `selfcheck.py` (one JSON LLM call), `pipeline.py` (`run_agent` orchestrator). `run_agent` returns the SAME `(suit, client, memo, confidence)` tuple as `generate_ticket_summary`, so posting, feedback buttons, and Phase 0A tracing are unchanged. When `agent_enabled`, `run_agent` writes the full `ai_suggestions` row (action_type, self_check, retrieved_refs) and `register_feedback_pending` skips its own record to avoid a duplicate. Kill switch: `agent_enabled=False` → old `generate_ticket_summary` runs, zero new behavior.

**Tech Stack:** Python 3, aiohttp/Groq (llama-3.3-70b for draft, gpt-oss-120b for self-check via existing transports), pytest (`asyncio_mode = auto`).

## Global Constraints

- No new third-party dependencies.
- **Kill switch is real**: with `config.agent_enabled=False` (default) the live flow calls the unchanged `generate_ticket_summary`; `run_agent` is never entered. Every integration branch checks `config.agent_enabled and config.agent_auto_first_suggestion_enabled`.
- **Same output contract**: `run_agent(...) -> tuple[str, str, str, int] | None` — `(suit_line, client_line, memo_line, confidence_pct)`, identical shape to `generate_ticket_summary` (`bot/ai_summary.py:603`). Returning `None` falls back to the old path.
- **Reuse, don't rewrite** context collection: import `_build_history_text`, `get_rag_context`, `_detect_equipment`, `get_active_format_instructions`, `_build_system_prompt`, `call_groq_text` from `bot/ai_summary.py`; `get_wiki_context` from `bot/wiki/searcher.py`; `find_solution_pattern` from `bot/db`. Do not duplicate their logic.
- **Action set** exactly: `ANSWER | ASK | ESCALATE | NO_ACTION` (reuse the tuple from `bot/optimizer/golden.ACTIONS`? No — define `AGENT_ACTIONS` locally in `bot/agent/actions.py` to avoid coupling agent runtime to the offline optimizer module).
- **Two-level safety**: `pre_generation_policy_check` (code, before generation) short-circuits to ESCALATE; `post_generation_safety_check` (code, on the generated client text) can force ESCALATE after generation. Both from `bot/agent/safety.py` (Phase 0A).
- **Self-check is ONE LLM call** returning `{status, fallback_action, fallback_client_text}`; on `unsupported` the pipeline publishes the fallback (ASK/ESCALATE) — no second generation.
- All LLM calls injectable via keyword-only `_draft_fn` / `_selfcheck_fn` for tests; tests never hit the network.
- `ai_suggestions` tracing: `run_agent` records ONE row with real `action_type`, `self_check` (JSON), `retrieved_refs` (JSON), `pipeline_version=config.agent_pipeline_version`, `prompt_version` (active prompt tag or "legacy"), `confidence`, `confidence_reason`. `register_feedback_pending` records nothing when `agent_enabled` (UI pending row only) to keep the idempotency_key unique.
- Freshness check is OUT of scope for Phase 1 (first suggestion fires synchronously right after topic creation — no window for supersession); it lands in Phase 3 (multi-turn by button). Documented in Out of scope.
- Dynamic few-shot from `dialogue_pairs` is Phase 2B — Phase 1 retrieval uses global KB only (`get_rag_context`), gated by `agent_dynamic_fewshot_enabled` (default off, no-op here).
- Tests: `asyncio_mode = auto`, new files under `tests/test_agent_*.py`.
- Style: `from __future__ import annotations`, RU docstrings/comments.

## Out of scope

Freshness/supersession (Phase 3), dynamic few-shot retrieval from dialogue_pairs (Phase 2B), transcript-scoped retrieval (Phase 4), the nightly judge (Phase 5B), any change to the posting/button code (Phase 0A already wired events; the tuple contract keeps it working).

## File Structure

- Create: `bot/agent/actions.py` — `AGENT_ACTIONS`, `build_action_instruction`, `parse_agent_draft`, `compose_memo`, `extract_client_text`.
- Create: `bot/agent/context.py` — `build_agent_context` (reuses ai_summary collectors), token budget.
- Create: `bot/agent/generate.py` — `generate_agent_draft`.
- Create: `bot/agent/selfcheck.py` — `self_check`.
- Create: `bot/agent/pipeline.py` — `run_agent`.
- Modify: `bot/db/suggestion_store.py` — extend `record_suggestion` with agent fields; re-export unchanged.
- Modify: `bot/handlers/ai_feedback.py::register_feedback_pending` — skip record when `agent_enabled`.
- Modify: `bot/topic_manager.py::_generate_summary_with_retry` — branch to `run_agent` when enabled.
- Test: `tests/test_agent_actions.py`, `tests/test_agent_context.py`, `tests/test_agent_generate.py`, `tests/test_agent_selfcheck.py`, `tests/test_agent_pipeline.py`, `tests/test_agent_integration.py`.

---

## Task 1: Pure action helpers

**Files:**
- Create: `bot/agent/actions.py`
- Test: `tests/test_agent_actions.py`

**Interfaces:**
- Produces:
  - `AGENT_ACTIONS = ("ANSWER", "ASK", "ESCALATE", "NO_ACTION")`
  - `build_action_instruction() -> str` — appended to the system prompt; instructs the model to pick an action and return a strict JSON object.
  - `parse_agent_draft(raw: str) -> dict | None` — parses the model's JSON (tolerant of ```json fences); requires `action ∈ AGENT_ACTIONS` and string `suit`/`client`/`memo`; `confidence` coerced to int 0..100 (default 50). None on malformed.
  - `extract_client_text(posts, client_id) -> str` — last client post text (HTML-stripped), "" if none.
  - `compose_memo(base_memo, *, grounds, confidence, missing, self_check_status, action) -> str` — memo + grounds (sources used), confidence, what's missing, self-check status.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_actions.py
from types import SimpleNamespace

from bot.agent.actions import (
    AGENT_ACTIONS,
    build_action_instruction,
    compose_memo,
    extract_client_text,
    parse_agent_draft,
)


def test_parse_agent_draft_ok_with_fences():
    raw = '```json\n{"action":"ASK","suit":"с","client":"какая модель?","memo":"м","confidence":70}\n```'
    d = parse_agent_draft(raw)
    assert d["action"] == "ASK"
    assert d["client"] == "какая модель?"
    assert d["confidence"] == 70


def test_parse_agent_draft_rejects_bad_action_and_malformed():
    assert parse_agent_draft('{"action":"MAYBE","suit":"s","client":"c","memo":"m"}') is None
    assert parse_agent_draft("не json") is None


def test_parse_agent_draft_defaults_confidence():
    d = parse_agent_draft('{"action":"ANSWER","suit":"s","client":"c","memo":"m"}')
    assert d["confidence"] == 50


def test_build_action_instruction_lists_actions():
    instr = build_action_instruction()
    for a in AGENT_ACTIONS:
        assert a in instr
    assert "JSON" in instr


def test_extract_client_text_last_client_post():
    posts = [
        SimpleNamespace(user_id=1, text="<p>первый вопрос</p>"),
        SimpleNamespace(user_id=99, text="ответ оператора"),
        SimpleNamespace(user_id=1, text="<b>второй вопрос</b>"),
    ]
    assert extract_client_text(posts, client_id=1) == "второй вопрос"
    assert extract_client_text([], client_id=1) == ""


def test_compose_memo_includes_grounds_and_status():
    memo = compose_memo(
        "перезагрузите кассу",
        grounds=["KB#12", "wiki:Чек не печатает"],
        confidence=80, missing="модель ОФД",
        self_check_status="supported", action="ANSWER",
    )
    assert "перезагрузите кассу" in memo
    assert "KB#12" in memo
    assert "модель ОФД" in memo
    assert "ANSWER" in memo
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
        '"memo": "шпаргалка оператору", "confidence": 0-100}'
    )


def parse_agent_draft(raw: str) -> dict | None:
    """Парсит JSON-ответ модели (терпим к ```json ограждениям)."""
    if not raw:
        return None
    text = raw.strip()
    fence = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not fence:
        return None
    try:
        obj = json.loads(fence.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("action") not in AGENT_ACTIONS:
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
) -> str:
    """Памятка оператору + основания: источники, уверенность, чего не хватает."""
    lines = [base_memo.strip()] if base_memo.strip() else []
    lines.append(f"Действие: {action} · уверенность {confidence}% · self-check: {self_check_status}")
    if grounds:
        lines.append("Основания: " + "; ".join(grounds))
    if missing:
        lines.append("Не хватает: " + missing)
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_actions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/actions.py tests/test_agent_actions.py
git commit -m "feat(agent): pure action helpers — instruction, parse, memo"
```

---

## Task 2: ContextBuilder

**Files:**
- Create: `bot/agent/context.py`
- Test: `tests/test_agent_context.py`

**Interfaces:**
- Consumes (injectable, defaults reuse ai_summary): `_history_fn=_build_history_text`, `_rag_fn=get_rag_context`, `_equipment_fn=_detect_equipment`, `_wiki_fn=get_wiki_context`, `_pattern_fn=find_solution_pattern`.
- Produces: `build_agent_context(posts, info, ticket_title, company_id="", *, history_budget=2000, _history_fn=None, _rag_fn=None, _equipment_fn=None, _wiki_fn=None, _pattern_fn=None) -> dict` — `{history, client_text, equipment, rag_examples, wiki, solution_steps, grounds, confidence}`. `history` trimmed to last `history_budget` chars. `grounds` lists source labels actually used (e.g. `KB#<n>`, `wiki:<title>`, `pattern:<equipment>`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_context.py
from types import SimpleNamespace

from bot.agent.context import build_agent_context


async def test_build_agent_context_collects_and_budgets():
    posts = [
        SimpleNamespace(user_id=1, text="касса не печатает чек"),
        SimpleNamespace(user_id=99, text="проверьте бумагу"),
    ]
    info = SimpleNamespace(client_id=1)

    def fake_history(p, i):
        return "Клиент: касса не печатает чек\nСотрудник: проверьте бумагу " + "x" * 5000

    async def fake_rag(title, tail, *, limit=3, company_id=""):
        return (["Пример из базы"], 88)

    def fake_equipment(title, history):
        return "АТОЛ"

    async def fake_wiki(title):
        return "Статья про чеки"

    async def fake_pattern(equipment, keywords):
        return {"steps": "1. Проверить бумагу", "id": 3}

    ctx = await build_agent_context(
        posts, info, "Не печатает чек", company_id="c1",
        _history_fn=fake_history, _rag_fn=fake_rag, _equipment_fn=fake_equipment,
        _wiki_fn=fake_wiki, _pattern_fn=fake_pattern,
    )
    assert len(ctx["history"]) <= 2000                 # бюджет истории
    assert ctx["client_text"] == "касса не печатает чек"
    assert ctx["equipment"] == "АТОЛ"
    assert ctx["rag_examples"] == ["Пример из базы"]
    assert ctx["confidence"] == 88
    assert "Статья про чеки" == ctx["wiki"]
    assert ctx["solution_steps"] == "1. Проверить бумагу"
    assert any("wiki" in g for g in ctx["grounds"])
    assert any(g.startswith("KB") for g in ctx["grounds"])


async def test_build_agent_context_handles_empty_retrieval():
    posts = [SimpleNamespace(user_id=1, text="вопрос")]
    info = SimpleNamespace(client_id=1)

    async def empty_rag(title, tail, *, limit=3, company_id=""):
        return ([], 0)

    async def none_wiki(title):
        return None

    async def none_pattern(equipment, keywords):
        return None

    ctx = await build_agent_context(
        posts, info, "t",
        _history_fn=lambda p, i: "Клиент: вопрос", _rag_fn=empty_rag,
        _equipment_fn=lambda t, h: None, _wiki_fn=none_wiki, _pattern_fn=none_pattern,
    )
    assert ctx["rag_examples"] == []
    assert ctx["wiki"] is None
    assert ctx["solution_steps"] is None
    assert ctx["grounds"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_context.py -v`
Expected: FAIL — `No module named 'bot.agent.context'`.

- [ ] **Step 3: Implement**

```python
# bot/agent/context.py
"""Сборка контекста для агента. Переиспользует сборщики bot/ai_summary.py,
добавляет бюджет истории и список оснований (grounds)."""
from __future__ import annotations

from .actions import extract_client_text


async def build_agent_context(
    posts,
    info,
    ticket_title: str,
    company_id: str = "",
    *,
    history_budget: int = 2000,
    _history_fn=None,
    _rag_fn=None,
    _equipment_fn=None,
    _wiki_fn=None,
    _pattern_fn=None,
) -> dict:
    if _history_fn is None:
        from ..ai_summary import _build_history_text as _history_fn
    if _rag_fn is None:
        from ..knowledge.indexer import get_rag_context as _rag_fn
    if _equipment_fn is None:
        from ..ai_summary import _detect_equipment as _equipment_fn
    if _wiki_fn is None:
        from ..wiki.searcher import get_wiki_context as _wiki_fn
    if _pattern_fn is None:
        from ..db import find_solution_pattern as _pattern_fn

    history = _history_fn(posts, info)[-history_budget:]
    client_text = extract_client_text(posts, getattr(info, "client_id", ""))
    equipment = _equipment_fn(ticket_title, history)

    rag_examples, confidence = await _rag_fn(
        ticket_title, history[-600:], limit=3, company_id=company_id
    )
    wiki = await _wiki_fn(ticket_title)
    pattern = await _pattern_fn(equipment, ticket_title)
    solution_steps = pattern.get("steps") if pattern else None

    grounds: list[str] = []
    for i, _ in enumerate(rag_examples):
        grounds.append(f"KB#{i + 1}")
    if wiki:
        grounds.append(f"wiki:{ticket_title[:40]}")
    if solution_steps:
        grounds.append(f"pattern:{equipment or '?'}")

    return {
        "history": history,
        "client_text": client_text,
        "equipment": equipment,
        "rag_examples": rag_examples,
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
git commit -m "feat(agent): ContextBuilder reusing ai_summary collectors"
```

---

## Task 3: Draft generation

**Files:**
- Create: `bot/agent/generate.py`
- Test: `tests/test_agent_generate.py`

**Interfaces:**
- Consumes: `bot.ai_summary._build_system_prompt`, `bot.ai_summary.get_active_format_instructions`, `bot.ai_summary.call_groq_text`; `build_action_instruction`/`parse_agent_draft` (Task 1).
- Produces: `generate_agent_draft(context, ticket_title, *, _call_fn=None, _prompt_fn=None, _format_fn=None) -> dict | None` — builds system prompt (base `_build_system_prompt` + `build_action_instruction`), calls the LLM once with the history as user content, parses via `parse_agent_draft`. Returns the parsed dict or None.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_generate.py
from bot.agent.generate import generate_agent_draft

_CTX = {
    "history": "Клиент: касса не печатает чек",
    "client_text": "касса не печатает чек",
    "equipment": "АТОЛ", "rag_examples": ["Пример"], "wiki": "Статья",
    "solution_steps": "1. Проверить бумагу", "grounds": ["KB#1"], "confidence": 80,
}


async def test_generate_agent_draft_parses_model_json():
    async def fake_call(prompt, *, system=None, model=None, max_tokens=None, temperature=None):
        return '{"action":"ANSWER","suit":"чек","client":"проверьте бумагу","memo":"м","confidence":85}'

    def fake_prompt(title, rag_examples=None, wiki_context=None, *, equipment=None, solution_steps=None, format_instructions=None):
        return "SYSTEM"

    async def fake_format():
        return "FORMAT"

    draft = await generate_agent_draft(
        _CTX, "Не печатает чек",
        _call_fn=fake_call, _prompt_fn=fake_prompt, _format_fn=fake_format,
    )
    assert draft["action"] == "ANSWER"
    assert draft["client"] == "проверьте бумагу"
    assert draft["confidence"] == 85


async def test_generate_agent_draft_none_on_unparseable():
    async def bad_call(prompt, *, system=None, model=None, max_tokens=None, temperature=None):
        return "мусор без json"

    async def fake_format():
        return "FORMAT"

    draft = await generate_agent_draft(
        _CTX, "t",
        _call_fn=bad_call, _prompt_fn=lambda *a, **k: "S", _format_fn=fake_format,
    )
    assert draft is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_generate.py -v`
Expected: FAIL — `No module named 'bot.agent.generate'`.

- [ ] **Step 3: Implement**

```python
# bot/agent/generate.py
"""Черновая генерация агента: system-промпт (база + инструкция действия) +
один LLM-вызов, парсинг строгого JSON."""
from __future__ import annotations

from .actions import build_action_instruction, parse_agent_draft

_MODEL = "llama-3.3-70b-versatile"


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

    format_instructions = await _format_fn()
    base_system = _prompt_fn(
        ticket_title,
        rag_examples=context.get("rag_examples") or None,
        wiki_context=context.get("wiki"),
        equipment=context.get("equipment"),
        solution_steps=context.get("solution_steps"),
        format_instructions=format_instructions,
    )
    system = base_system + build_action_instruction()
    raw = await _call_fn(
        context["history"], system=system, model=_MODEL,
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
git commit -m "feat(agent): draft generation with action selection"
```

---

## Task 4: Self-check (one LLM call)

**Files:**
- Create: `bot/agent/selfcheck.py`
- Test: `tests/test_agent_selfcheck.py`

**Interfaces:**
- Consumes: `bot.optimizer.judge._call_groq_judge` (default JSON transport, gpt-oss-120b).
- Produces: `self_check(client_text, generated_client, grounds, history, *, _call_fn=None) -> dict` — one call; returns `{"status": "supported"|"partially_supported"|"unsupported", "fallback_action": "ASK"|"ESCALATE", "fallback_client_text": str}`. On unparseable/failed call, returns a conservative default `{"status": "unsupported", "fallback_action": "ASK", "fallback_client_text": ""}` (never raises).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_selfcheck.py
import json

from bot.agent.selfcheck import self_check


async def test_self_check_supported():
    async def fake_call(system, user):
        return json.dumps({"status": "supported", "fallback_action": "ASK",
                           "fallback_client_text": ""})

    r = await self_check("вопрос", "ответ", ["KB#1"], "история", _call_fn=fake_call)
    assert r["status"] == "supported"


async def test_self_check_unsupported_with_fallback():
    async def fake_call(system, user):
        return json.dumps({"status": "unsupported", "fallback_action": "ASK",
                           "fallback_client_text": "уточните модель кассы"})

    r = await self_check("вопрос", "выдуманный ответ", [], "история", _call_fn=fake_call)
    assert r["status"] == "unsupported"
    assert r["fallback_action"] == "ASK"
    assert r["fallback_client_text"] == "уточните модель кассы"


async def test_self_check_conservative_default_on_bad_json():
    async def bad_call(system, user):
        return "не json"

    r = await self_check("в", "о", [], "и", _call_fn=bad_call)
    assert r["status"] == "unsupported"
    assert r["fallback_action"] == "ASK"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_selfcheck.py -v`
Expected: FAIL — `No module named 'bot.agent.selfcheck'`.

- [ ] **Step 3: Implement**

```python
# bot/agent/selfcheck.py
"""Self-check одним LLM-вызовом: опираются ли утверждения ответа на контекст.
При невалидном ответе — консервативный дефолт unsupported→ASK (не падает)."""
from __future__ import annotations

import json

_DEFAULT = {"status": "unsupported", "fallback_action": "ASK", "fallback_client_text": ""}
_STATUSES = ("supported", "partially_supported", "unsupported")


def _build_selfcheck_prompt(client_text, generated_client, grounds, history):
    system = (
        "Ты проверяешь ответ техподдержки на галлюцинации и безопасность. "
        "Опираются ли фактические утверждения ответа на предоставленный контекст "
        "(история тикета + найденные источники)? Не выдуманы ли шаги?\n"
        "status: supported — всё подтверждается; partially_supported — часть без опоры; "
        "unsupported — ключевые утверждения не подтверждаются или ответ небезопасен.\n"
        "Если status != supported, предложи безопасный fallback: ASK (уточнить) или "
        "ESCALATE (передать оператору) и текст fallback_client_text для клиента.\n"
        'Верни СТРОГО JSON: {"status":"...","fallback_action":"ASK|ESCALATE",'
        '"fallback_client_text":"..."}'
    )
    user = (
        f"Вопрос клиента: {client_text}\n\n"
        f"История:\n{history}\n\n"
        f"Найденные источники (grounds): {', '.join(grounds) if grounds else 'нет'}\n\n"
        f"Ответ кандидата клиенту:\n{generated_client}"
    )
    return system, user


async def self_check(
    client_text: str,
    generated_client: str,
    grounds: list[str],
    history: str,
    *,
    _call_fn=None,
) -> dict:
    if _call_fn is None:
        from ..optimizer.judge import _call_groq_judge as _call_fn
    system, user = _build_selfcheck_prompt(client_text, generated_client, grounds, history)
    try:
        raw = await _call_fn(system, user)
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
git commit -m "feat(agent): single-call self-check with conservative fallback"
```

---

## Task 5: Extend `record_suggestion` with agent fields

**Files:**
- Modify: `bot/db/suggestion_store.py`
- Test: `tests/test_suggestion_store.py` (append)

**Interfaces:**
- Produces: `record_suggestion` gains keyword-only optional params `action_type=None`, `self_check=None` (JSON str), `retrieved_refs=None` (JSON str), `confidence=None`, `confidence_reason=None`. Backward-compatible: existing callers unaffected; new params written to the matching columns (all already exist from Phase 0A schema).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_suggestion_store.py (append)
async def test_record_suggestion_stores_agent_fields():
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="TA", topic_id=9, trigger_source="first",
        context_until_post_id="50", pipeline_version="v1", prompt_version="legacy",
        action_type="ASK", self_check='{"status":"unsupported"}',
        retrieved_refs='[{"source_type":"kb","source_id":12}]',
        confidence=65, confidence_reason="мало данных",
    )
    row = await get_suggestion(sid)
    assert row["action_type"] == "ASK"
    assert row["self_check"] == '{"status":"unsupported"}'
    assert row["retrieved_refs"] == '[{"source_type":"kb","source_id":12}]'
    assert row["confidence"] == 65
    assert row["confidence_reason"] == "мало данных"
```

(reuses `record_suggestion`, `get_suggestion`, `db_module` already imported at the top of `tests/test_suggestion_store.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_suggestion_store.py -k agent_fields -v`
Expected: FAIL — `record_suggestion() got an unexpected keyword argument 'action_type'`.

- [ ] **Step 3: Implement**

In `bot/db/suggestion_store.py`, extend `record_suggestion`'s signature (add after `model: str | None = None`):

```python
    action_type: str | None = None,
    self_check: str | None = None,
    retrieved_refs: str | None = None,
    confidence: int | None = None,
    confidence_reason: str | None = None,
```

And extend the INSERT column list + values to include these five columns:

```python
        await db.execute(
            "INSERT OR IGNORE INTO ai_suggestions "
            "(ticket_id, topic_id, trigger_source, context_until_post_id, client_id, "
            " idempotency_key, title, history, client_text, ai_answer, ai_full_text, "
            " pipeline_version, prompt_version, model, action_type, self_check, "
            " retrieved_refs, confidence, confidence_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_id, topic_id, trigger_source, context_until_post_id, client_id,
                key, title, history, client_text, ai_answer, ai_full_text,
                pipeline_version, prompt_version, model, action_type, self_check,
                retrieved_refs, confidence, confidence_reason,
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_suggestion_store.py -v`
Expected: PASS (all, incl. existing Phase 0A tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add bot/db/suggestion_store.py tests/test_suggestion_store.py
git commit -m "feat(agent): record_suggestion stores action_type/self_check/refs"
```

---

## Task 6: Pipeline orchestrator `run_agent`

**Files:**
- Create: `bot/agent/pipeline.py`
- Test: `tests/test_agent_pipeline.py`

**Interfaces:**
- Consumes: `pre_generation_policy_check`/`post_generation_safety_check` (safety), `build_agent_context`, `generate_agent_draft`, `self_check`, `compose_memo`, `bot.db.record_suggestion`.
- Produces: `run_agent(posts, info, *, ticket_title, ticket_id, company_id="", _context_fn=None, _draft_fn=None, _selfcheck_fn=None, _safety_pre=None, _safety_post=None, _record_fn=None) -> tuple[str, str, str, int] | None`. Orchestration:
  1. context = build_agent_context(...); `client_text = context["client_text"]`.
  2. pre-policy on client_text → ESCALATE short-circuit (no generation).
  3. else draft = generate_agent_draft(...); if None → return None (fallback to old path).
  4. post-safety on draft["client"] → force ESCALATE if flagged.
  5. if action == ANSWER → self_check; if `unsupported` → switch to fallback_action, replace client with fallback_client_text.
  6. compose memo with grounds/confidence/self-check status.
  7. record_suggestion with full agent fields (non-fatal).
  8. return (suit, client, memo, confidence).
- Records to `ai_suggestions` with `context_until_post_id = str(max post_id)`, `pipeline_version=config.agent_pipeline_version`, `trigger_source="first"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_pipeline.py
from types import SimpleNamespace

from bot.agent.pipeline import run_agent
from bot.agent.safety import PolicyDecision

_POSTS = [SimpleNamespace(user_id=1, text="касса не печатает", post_id=5)]
_INFO = SimpleNamespace(client_id=1)


async def _ctx(*a, **k):
    return {"history": "Клиент: касса не печатает", "client_text": "касса не печатает",
            "equipment": "АТОЛ", "rag_examples": ["п"], "wiki": None,
            "solution_steps": None, "grounds": ["KB#1"], "confidence": 80}


async def test_run_agent_answer_path_records_and_returns_tuple():
    recorded = {}

    async def draft(ctx, title, **k):
        return {"action": "ANSWER", "suit": "чек", "client": "проверьте бумагу",
                "memo": "м", "confidence": 85}

    async def sc(ct, gen, grounds, hist, **k):
        return {"status": "supported", "fallback_action": "ASK", "fallback_client_text": ""}

    async def rec(**kw):
        recorded.update(kw)
        return 1

    result = await run_agent(
        _POSTS, _INFO, ticket_title="Не печатает", ticket_id="T1",
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=sc,
        _safety_pre=lambda t: PolicyDecision("PROCEED", None, None),
        _safety_post=lambda t: PolicyDecision("PROCEED", None, None),
        _record_fn=rec,
    )
    suit, client, memo, conf = result
    assert client == "проверьте бумагу"
    assert conf == 85
    assert "KB#1" in memo                         # grounds in memo
    assert recorded["action_type"] == "ANSWER"
    assert recorded["context_until_post_id"] == "5"
    assert recorded["ai_answer"] == "проверьте бумагу"


async def test_run_agent_pre_policy_escalates_without_generation():
    draft_called = {"v": False}

    async def draft(ctx, title, **k):
        draft_called["v"] = True
        return {"action": "ANSWER", "suit": "s", "client": "сделаю возврат", "memo": "m", "confidence": 90}

    async def rec(**kw):
        return 1

    result = await run_agent(
        _POSTS, _INFO, ticket_title="Возврат", ticket_id="T2",
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=None,
        _safety_pre=lambda t: PolicyDecision("ESCALATE", "finance", "верн.*деньг"),
        _safety_post=lambda t: PolicyDecision("PROCEED", None, None),
        _record_fn=rec,
    )
    suit, client, memo, conf = result
    assert draft_called["v"] is False             # генерации не было
    assert client == ""                           # клиенту ничего не отправляем
    assert "ESCALATE" in memo


async def test_run_agent_unsupported_selfcheck_switches_to_fallback():
    async def draft(ctx, title, **k):
        return {"action": "ANSWER", "suit": "s", "client": "выдуманный ответ", "memo": "m", "confidence": 70}

    async def sc(ct, gen, grounds, hist, **k):
        return {"status": "unsupported", "fallback_action": "ASK",
                "fallback_client_text": "уточните модель кассы"}

    async def rec(**kw):
        return 1

    result = await run_agent(
        _POSTS, _INFO, ticket_title="t", ticket_id="T3",
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=sc,
        _safety_pre=lambda t: PolicyDecision("PROCEED", None, None),
        _safety_post=lambda t: PolicyDecision("PROCEED", None, None),
        _record_fn=rec,
    )
    _, client, memo, _ = result
    assert client == "уточните модель кассы"      # fallback заменил ответ
    assert "ASK" in memo


async def test_run_agent_returns_none_on_draft_failure():
    async def draft(ctx, title, **k):
        return None

    async def rec(**kw):
        return 1

    result = await run_agent(
        _POSTS, _INFO, ticket_title="t", ticket_id="T4",
        _context_fn=_ctx, _draft_fn=draft, _selfcheck_fn=None,
        _safety_pre=lambda t: PolicyDecision("PROCEED", None, None),
        _safety_post=lambda t: PolicyDecision("PROCEED", None, None),
        _record_fn=rec,
    )
    assert result is None                          # → откат на старый путь
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_pipeline.py -v`
Expected: FAIL — `No module named 'bot.agent.pipeline'`.

- [ ] **Step 3: Implement**

```python
# bot/agent/pipeline.py
"""Оркестратор агентного пайплайна (Phase 1).

pre-policy → context → генерация → post-safety → self-check → Памятка → запись.
Возвращает тот же кортеж, что generate_ticket_summary, или None (откат)."""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def _anchor_post_id(posts) -> str | None:
    ids = [getattr(p, "post_id", None) for p in posts]
    ids = [int(i) for i in ids if i is not None]
    return str(max(ids)) if ids else None


async def run_agent(
    posts,
    info,
    *,
    ticket_title: str,
    ticket_id: str,
    company_id: str = "",
    _context_fn=None,
    _draft_fn=None,
    _selfcheck_fn=None,
    _safety_pre=None,
    _safety_post=None,
    _record_fn=None,
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

    from .actions import compose_memo

    context = await _context_fn(posts, info, ticket_title, company_id)
    client_text = context["client_text"]
    grounds = context["grounds"]
    history = context["history"]

    action = "ANSWER"
    suit = ""
    client = ""
    base_memo = ""
    confidence = context.get("confidence", 50)
    self_status = "n/a"

    pre = _safety_pre(client_text)
    if pre.action == "ESCALATE":
        action = "ESCALATE"
        base_memo = f"⚠️ Эскалация ({pre.category}): вопрос требует оператора."
        confidence = 0
    else:
        draft = await _draft_fn(context, ticket_title)
        if draft is None:
            return None  # откат на старый путь
        action = draft["action"]
        suit = draft["suit"]
        client = draft["client"]
        base_memo = draft["memo"]
        confidence = draft["confidence"]

        post = _safety_post(client)
        if post.action == "ESCALATE":
            action = "ESCALATE"
            client = ""
            base_memo = f"⚠️ Эскалация ({post.category}): предложенный ответ небезопасен. " + base_memo
            confidence = 0
        elif action == "ANSWER":
            check = await _selfcheck_fn(client_text, client, grounds, history)
            self_status = check["status"]
            if self_status == "unsupported":
                action = check["fallback_action"]
                client = check["fallback_client_text"]

    missing = "" if action == "ANSWER" else "нужны уточнения/оператор"
    memo = compose_memo(
        base_memo, grounds=grounds, confidence=confidence, missing=missing,
        self_check_status=self_status, action=action,
    )

    try:
        from ..config import config
        refs = [{"label": g} for g in grounds]
        await _record_fn(
            ticket_id=ticket_id,
            topic_id=None,
            trigger_source="first",
            context_until_post_id=_anchor_post_id(posts),
            pipeline_version=config.agent_pipeline_version,
            prompt_version="legacy",
            title=ticket_title,
            history=history,
            client_text=client_text,
            ai_answer=client,
            ai_full_text=f"{suit}\n{client}\n{memo}",
            action_type=action,
            self_check=json.dumps({"status": self_status}, ensure_ascii=False),
            retrieved_refs=json.dumps(refs, ensure_ascii=False),
            confidence=confidence,
        )
    except Exception as exc:
        logger.warning("run_agent: suggestion record failed: %s", exc)

    return suit, client, memo, confidence
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_pipeline.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/agent/pipeline.py tests/test_agent_pipeline.py
git commit -m "feat(agent): run_agent pipeline orchestrator"
```

---

## Task 7: Integration — flag branch + register skip

**Files:**
- Modify: `bot/topic_manager.py` (`_generate_summary_with_retry`, ~:192)
- Modify: `bot/handlers/ai_feedback.py` (`register_feedback_pending`)
- Test: `tests/test_agent_integration.py`

**Interfaces:**
- Consumes: `run_agent`, `config.agent_enabled`/`agent_auto_first_suggestion_enabled`.
- Produces:
  - `_generate_summary_with_retry` calls `run_agent` when `config.agent_enabled and config.agent_auto_first_suggestion_enabled`; a `None` result (or exception) falls back to the existing `generate_ticket_summary` loop.
  - `register_feedback_pending` skips its own `record_suggestion` when `config.agent_enabled` (still saves the UI pending row) — the agent already recorded the row, so this avoids a second row under a different idempotency_key.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_integration.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot.config as config_module
import bot.db as db_module
from bot.db.suggestion_store import get_open_suggestion_by_topic
from bot.handlers.ai_feedback import register_feedback_pending


async def test_register_skips_record_when_agent_enabled(monkeypatch):
    await db_module.init_db()
    monkeypatch.setattr(config_module.config, "agent_enabled", True)
    await register_feedback_pending(
        topic_id=1, ticket_id="T1", history="h", title="t",
        answer_text="a", ai_full_text="f",
    )
    # agent on → register did NOT create an ai_suggestions row
    assert await get_open_suggestion_by_topic(1) is None


async def test_register_records_when_agent_disabled(monkeypatch):
    await db_module.init_db()
    monkeypatch.setattr(config_module.config, "agent_enabled", False)
    await register_feedback_pending(
        topic_id=2, ticket_id="T2", history="h", title="t",
        answer_text="a", ai_full_text="f",
    )
    row = await get_open_suggestion_by_topic(2)
    assert row is not None and row["ticket_id"] == "T2"


async def test_generate_with_retry_uses_agent_when_enabled(monkeypatch):
    import bot.topic_manager as tm
    monkeypatch.setattr(config_module.config, "agent_enabled", True)
    monkeypatch.setattr(config_module.config, "agent_auto_first_suggestion_enabled", True)
    with patch("bot.topic_manager.run_agent", new=AsyncMock(
        return_value=("суть", "клиенту", "памятка", 88)
    )) as mock_agent, patch(
        "bot.topic_manager.generate_ticket_summary", new=AsyncMock()
    ) as mock_old:
        result = await tm._generate_summary_with_retry(
            [SimpleNamespace(user_id=1, text="q", post_id=1)],
            SimpleNamespace(client_id=1),
            ticket_title="t", ticket_id="T3",
        )
    assert result == ("суть", "клиенту", "памятка", 88)
    mock_agent.assert_awaited()
    mock_old.assert_not_awaited()                  # старый путь не вызывался
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_integration.py -v`
Expected: FAIL — `register` still records when agent enabled / `bot.topic_manager` has no `run_agent`.

- [ ] **Step 3: Implement**

**(a) `bot/handlers/ai_feedback.py::register_feedback_pending`** — guard the record block. In the existing `try:` that calls `record_suggestion` (added in Phase 0A), add an early skip:

```python
    try:
        from ..config import config
        if config.agent_enabled:
            return  # агент сам записывает ai_suggestions (Phase 1) — без дубля
        await record_suggestion(
            ...  # unchanged existing call
        )
    except Exception as exc:
        logger.warning("ai_feedback: suggestion record failed: %s", exc)
```

Note: the `save_ai_feedback_pending(...)` call above this `try` stays unconditional — the UI pending row is always written.

**(b) `bot/topic_manager.py`** — import `run_agent` at module top with the other agent-adjacent imports:

```python
from .agent.pipeline import run_agent
```

In `_generate_summary_with_retry` (~:192), before the existing retry loop that calls `generate_ticket_summary`, add the agent branch:

```python
async def _generate_summary_with_retry(
    posts, info, *, ticket_title="", ticket_id="", company_id="", attempts=3, pause=30.0
):
    from .config import config
    if config.agent_enabled and config.agent_auto_first_suggestion_enabled:
        try:
            result = await run_agent(
                posts, info, ticket_title=ticket_title,
                ticket_id=ticket_id, company_id=company_id,
            )
            if result is not None:
                return result
        except Exception as exc:
            logger.warning("run_agent failed, falling back to summary: %s", exc)
        # fall through to the legacy loop below on None/exception
    # ... existing generate_ticket_summary retry loop unchanged ...
```

Keep the existing loop body exactly as-is after this block. If `logger` is not already defined in `topic_manager.py`, use the module's existing logger reference (do not add a new one — match the file).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_integration.py -v`
Expected: PASS.

- [ ] **Step 5: Regression + commit**

Run: `python -m pytest tests/test_topic_manager.py tests/test_ai_feedback_flow.py tests/test_ai_feedback_events.py -v`
Expected: PASS (agent flags default off → existing flows unchanged).

```bash
git add bot/topic_manager.py bot/handlers/ai_feedback.py tests/test_agent_integration.py
git commit -m "feat(agent): wire run_agent behind flag, skip dup record"
```

---

## Task 8: Full-suite verification

- [ ] **Step 1:** `python -m pytest -q` — expected PASS, no regressions.
- [ ] **Step 2:** `python -c "import bot.main" 2>&1 | tail -1` — expected clean import (agent pipeline wired into topic_manager without import cycles).
- [ ] **Step 3:** Verify default-off behavior: with no env set, `python -c "import bot.config as c; print(c.config.agent_enabled)"` prints `False` (live flow keeps using `generate_ticket_summary`).
- [ ] **Step 4:** Commit any fixups: `git commit -m "test(agent): phase 1 verification" --allow-empty`.

---

## Self-Review

**Spec coverage (Phase 1):**
- Agentic pipeline replacing single-shot generation, behind AGENT_ENABLED → Tasks 6–7. ✓
- Order: policy pre-check → context → retrieval → action choice → generation → self-check → publish → Tasks 2/3/4/6. ✓
- Four actions ANSWER/ASK/ESCALATE/NO_ACTION → Task 1 (`AGENT_ACTIONS`), Task 6 (selection/short-circuit). ✓
- Adaptive format, ≤20-word limit removed → Task 3 (draft prompt has no word cap; action instruction drives ANSWER vs ASK). ✓
- Two-level safety, code-driven → Task 6 (pre + post via `bot/agent/safety.py`). ✓
- Self-check = one LLM call, unsupported → fallback without regeneration → Task 4 + Task 6 step 5. ✓
- Memo with grounds/confidence/missing → Task 1 `compose_memo`, Task 6. ✓
- Retrieval reuses global KB; dynamic few-shot gated off (Phase 2B) → Task 2 + Global Constraints. ✓
- Full `ai_suggestions` tracing with action_type/self_check/refs; no duplicate row → Task 5 + Task 6 + Task 7(a). ✓
- Same output contract / kill switch → Global Constraints, Task 7. ✓
- Freshness deferred to Phase 3 → Out of scope (documented with reason). ✓

**Placeholder scan:** clean. Task 7 Step 3 references "existing loop unchanged" — this is an instruction to preserve real existing code, not a placeholder; the new code around it is fully written.

**Type consistency:** `run_agent` returns `tuple[str,str,str,int]|None` matching `generate_ticket_summary` and the integration branch's expectation; `build_agent_context` dict keys (Task 2) match what `generate_agent_draft` (Task 3) and `run_agent` (Task 6) read; `parse_agent_draft` output keys (`action/suit/client/memo/confidence`) match `run_agent`'s draft usage; `self_check` return keys (`status/fallback_action/fallback_client_text`) match Task 6 step 5; `record_suggestion` extended kwargs (Task 5) match `run_agent`'s call.
