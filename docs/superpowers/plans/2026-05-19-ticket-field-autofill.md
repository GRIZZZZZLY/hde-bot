# HDE Ticket Field Auto-Fill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After the AI summary is generated for a ticket, automatically set HDE custom fields: Классификация→"Оборудование" (always), Окружение→LLM-classified value (overwrite always; warn in topic if undetermined), Роль→"Не важно" (only if currently empty).

**Architecture:** A dedicated module `bot/ticket_fields.py` holds the field/option ID maps, an isolated Gemini classifier for Окружение (separate from the DB-overridable summary prompt), and an orchestrator `apply_ticket_fields()`. The orchestrator is invoked from `_post_ticket_history()` in `topic_manager.py` after the summary posts, inside its own try/except so field-update failure never breaks the summary. A new read helper on `HDEApiClient` checks the current Роль value for the "only if empty" rule.

**Tech Stack:** Python 3.11+, aiohttp, aiogram 3.x, pytest + unittest.mock (AsyncMock), Google Gemini REST API.

---

## Field & Option Reference (discovered empirically via API scan of 450 tickets)

PUT format: `PUT /tickets/{id}/` body `{"custom_fields": {"<field_id>": "<option_id>"}}` (select fields → value is the option_id as string).

| Field | field_id | Action |
|---|---|---|
| Классификация | `3` | Always `20` (= "Оборудование") |
| Окружение | `2` | LLM picks option_id; overwrite always; if undetermined → topic warning, do not write field 2 |
| Роль | `24` | `197` (= "Не важно") only if current value empty (`field_value.id == 0`) |

Окружение option map (option_id → label), field_id 2:

```
15  Интернет витрина
11  POS
146 Эвотор
148 АКСИ \ AQSI
12  Биллинг
190 Wallet
10  Админ панель
145 Атол
17  API
140 ТГ-Бот
13  Florist
151 Яндекс Пэй
150 INPAS \ ИНПАС
46  Менеджер
149 SBER \ СБЕР
14  Другое
153 Принтер Этикеток
56  Интеграция
152 Принтер Чеков
157 WEB касса \ Веб касса
156 Viki Print \ Вики принт
```

Full map also saved at `docs/hde_custom_field_ids.json`.

---

## File Structure

- **Create** `bot/ticket_fields.py` — field/option constants, `OKRUZHENIE_OPTIONS`, `classify_environment()`, `apply_ticket_fields()`. Single responsibility: ticket custom-field auto-fill.
- **Modify** `bot/hde_api.py` — add `get_ticket_field_value(ticket_id, field_id)` read helper (~line 140, after `get_ticket_info`).
- **Modify** `bot/topic_manager.py` — call `apply_ticket_fields()` in `_post_ticket_history()` after the summary block (after line 599).
- **Create** `tests/test_ticket_fields.py` — unit tests for the new module.
- **Modify** `tests/test_topic_manager.py` — verify the hook fires after summary.

---

### Task 1: Field/option constants and option map

**Files:**
- Create: `bot/ticket_fields.py`
- Test: `tests/test_ticket_fields.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ticket_fields.py
from bot.ticket_fields import (
    FIELD_OKRUZHENIE,
    FIELD_KLASSIFIKACIYA,
    FIELD_ROL,
    KLASSIFIKACIYA_OBORUDOVANIE,
    ROL_NE_VAZHNO,
    OKRUZHENIE_OPTIONS,
)


def test_field_ids_are_strings():
    assert FIELD_OKRUZHENIE == "2"
    assert FIELD_KLASSIFIKACIYA == "3"
    assert FIELD_ROL == "24"


def test_fixed_option_ids():
    assert KLASSIFIKACIYA_OBORUDOVANIE == "20"
    assert ROL_NE_VAZHNO == "197"


def test_okruzhenie_options_complete():
    # 21 environments discovered from production scan
    assert len(OKRUZHENIE_OPTIONS) == 21
    assert OKRUZHENIE_OPTIONS["11"] == "POS"
    assert OKRUZHENIE_OPTIONS["146"] == "Эвотор"
    assert OKRUZHENIE_OPTIONS["14"] == "Другое"
    # all keys are numeric strings
    assert all(k.isdigit() for k in OKRUZHENIE_OPTIONS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ticket_fields.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.ticket_fields'`

- [ ] **Step 3: Write minimal implementation**

```python
# bot/ticket_fields.py
"""Auto-fill HDE ticket custom fields after AI summary.

Field & option IDs discovered empirically by scanning 450 production
tickets (HDE API does not expose select-field option lists).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Custom field IDs (HDE /custom_fields/ endpoint)
FIELD_OKRUZHENIE = "2"
FIELD_KLASSIFIKACIYA = "3"
FIELD_ROL = "24"

# Fixed option IDs
KLASSIFIKACIYA_OBORUDOVANIE = "20"  # "Оборудование"
ROL_NE_VAZHNO = "197"               # "Не важно"

# Окружение: option_id -> human label (field_id 2)
OKRUZHENIE_OPTIONS: dict[str, str] = {
    "15": "Интернет витрина",
    "11": "POS",
    "146": "Эвотор",
    "148": "АКСИ \\ AQSI",
    "12": "Биллинг",
    "190": "Wallet",
    "10": "Админ панель",
    "145": "Атол",
    "17": "API",
    "140": "ТГ-Бот",
    "13": "Florist",
    "151": "Яндекс Пэй",
    "150": "INPAS \\ ИНПАС",
    "46": "Менеджер",
    "149": "SBER \\ СБЕР",
    "14": "Другое",
    "153": "Принтер Этикеток",
    "56": "Интеграция",
    "152": "Принтер Чеков",
    "157": "WEB касса \\ Веб касса",
    "156": "Viki Print \\ Вики принт",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ticket_fields.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/ticket_fields.py tests/test_ticket_fields.py
git commit -m "feat(fields): add HDE custom-field id/option constants"
```

---

### Task 2: Окружение LLM classifier (isolated Gemini call)

**Files:**
- Modify: `bot/ticket_fields.py`
- Test: `tests/test_ticket_fields.py`

The classifier sends a short prompt listing the 21 options and the ticket conversation, asks Gemini to reply with EXACTLY one option_id or the literal token `НЕ ОПРЕДЕЛЕНО`. Returns the option_id string if valid and known, else `None`. Self-contained Gemini call — reuses `config.gemini_api_key` and the same model URL pattern as `ai_summary.py` but does NOT touch the summary prompt.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_ticket_fields.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.ticket_fields import classify_environment


def _gemini_resp(text: str):
    """Build a fake aiohttp response context manager returning Gemini JSON."""
    payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value="")
    resp.read = AsyncMock(return_value=b"")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_classify_environment_valid_id():
    session = MagicMock()
    session.post = MagicMock(return_value=_gemini_resp("11"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("Клиент: не открывается касса POS")
    assert result == "11"


@pytest.mark.asyncio
async def test_classify_environment_undetermined():
    session = MagicMock()
    session.post = MagicMock(return_value=_gemini_resp("НЕ ОПРЕДЕЛЕНО"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("Клиент: добрый день")
    assert result is None


@pytest.mark.asyncio
async def test_classify_environment_unknown_id_returns_none():
    session = MagicMock()
    session.post = MagicMock(return_value=_gemini_resp("99999"))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("bot.ticket_fields.aiohttp.ClientSession", return_value=sess_cm):
        result = await classify_environment("текст")
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ticket_fields.py -k classify -v`
Expected: FAIL — `ImportError: cannot import name 'classify_environment'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to bot/ticket_fields.py
import re

import aiohttp

from .config import config

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)


def _build_env_prompt() -> str:
    lines = "\n".join(
        f"{oid} = {label}" for oid, label in OKRUZHENIE_OPTIONS.items()
    )
    return (
        "Ты классифицируешь обращение в техподдержку кассового ПО/оборудования "
        "по полю «Окружение».\n"
        "Ниже список допустимых значений в формате «ID = Название»:\n\n"
        f"{lines}\n\n"
        "Прочитай переписку и определи наиболее подходящее окружение.\n"
        "Ответь СТРОГО одним токеном:\n"
        "— числовой ID из списка выше, ЕСЛИ окружение уверенно определяется;\n"
        "— либо ровно «НЕ ОПРЕДЕЛЕНО», если определить нельзя.\n"
        "Без пояснений, без префиксов, только токен."
    )


async def classify_environment(history: str) -> str | None:
    """Return an Окружение option_id, or None if undetermined / unknown / error."""
    if not config.gemini_api_key or not history.strip():
        return None
    payload = {
        "system_instruction": {"parts": [{"text": _build_env_prompt()}]},
        "contents": [{"parts": [{"text": f"Переписка:\n{history}"}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 16},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _GEMINI_URL,
                json=payload,
                params={"key": config.gemini_api_key},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Env classifier HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        logger.warning("Env classifier failed: %s", exc)
        return None
    m = re.search(r"\d+", raw)
    if not m:
        return None
    option_id = m.group(0)
    if option_id in OKRUZHENIE_OPTIONS:
        return option_id
    logger.info("Env classifier returned unknown id %r (raw=%r)", option_id, raw[:60])
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ticket_fields.py -k classify -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/ticket_fields.py tests/test_ticket_fields.py
git commit -m "feat(fields): add isolated Gemini Окружение classifier"
```

---

### Task 3: HDE read helper for current Роль value

**Files:**
- Modify: `bot/hde_api.py` (add method after `get_ticket_info`, ~line 140)
- Test: `tests/test_ticket_fields.py`

`get_ticket_field_value(ticket_id, field_id)` does `GET /tickets/{id}/`, finds the matching entry in `data.custom_fields` (a list of `{id, field_type, field_value}`), and returns the current option id as int. Select empty value is `field_value == {"id": 0, "name": null}` → returns `0`. Returns `None` on any error (fail-safe: caller then skips the Роль write to avoid clobbering).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_ticket_fields.py
from bot.hde_api import HDEApiClient


def _hde_get_resp(custom_fields: list):
    payload = {"data": {"custom_fields": custom_fields}}
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value="")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _patch_session(get_cm):
    session = MagicMock()
    session.get = MagicMock(return_value=get_cm)
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=session)
    sess_cm.__aexit__ = AsyncMock(return_value=False)
    return patch("bot.hde_api.aiohttp.ClientSession", return_value=sess_cm)


@pytest.mark.asyncio
async def test_get_ticket_field_value_empty():
    cf = [{"id": 24, "field_type": "select", "field_value": {"id": 0, "name": None}}]
    with _patch_session(_hde_get_resp(cf)):
        client = HDEApiClient()
        val = await client.get_ticket_field_value("123", 24)
    assert val == 0


@pytest.mark.asyncio
async def test_get_ticket_field_value_set():
    cf = [{"id": 24, "field_type": "select",
           "field_value": {"id": 199, "name": {"ru": "Администратор"}}}]
    with _patch_session(_hde_get_resp(cf)):
        client = HDEApiClient()
        val = await client.get_ticket_field_value("123", 24)
    assert val == 199


@pytest.mark.asyncio
async def test_get_ticket_field_value_missing_field():
    with _patch_session(_hde_get_resp([])):
        client = HDEApiClient()
        val = await client.get_ticket_field_value("123", 24)
    assert val == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ticket_fields.py -k field_value -v`
Expected: FAIL — `AttributeError: 'HDEApiClient' object has no attribute 'get_ticket_field_value'`

- [ ] **Step 3: Write minimal implementation**

Insert immediately after the `get_ticket_info` method (after its `return HDETicketInfo(...)` block, before `get_ticket_open_status`):

```python
    async def get_ticket_field_value(self, ticket_id: str, field_id: int) -> int | None:
        """Return the current select option id for a custom field.

        0 means the field is empty. None means an error occurred — caller
        must NOT assume the field is empty (fail-safe against clobbering).
        """
        url = f"{self.base_url}/tickets/{ticket_id}/"
        try:
            async with aiohttp.ClientSession(auth=self.auth) as session:
                async with session.get(url) as response:
                    data = await self._read_response(response)
                    if response.status >= 400:
                        return None
            raw = data.get("data", data) if isinstance(data, dict) else {}
            for cf in raw.get("custom_fields") or []:
                if cf.get("id") != field_id:
                    continue
                fv = cf.get("field_value")
                if isinstance(fv, dict):
                    return int(fv.get("id") or 0)
                return 0
            return 0
        except Exception:
            return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ticket_fields.py -k field_value -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/hde_api.py tests/test_ticket_fields.py
git commit -m "feat(hde): add get_ticket_field_value read helper"
```

---

### Task 4: `apply_ticket_fields` orchestrator

**Files:**
- Modify: `bot/ticket_fields.py`
- Test: `tests/test_ticket_fields.py`

`apply_ticket_fields(bot, ticket_id, topic_id, history)`:
1. Build `fields = {FIELD_KLASSIFIKACIYA: KLASSIFIKACIYA_OBORUDOVANIE}` (always).
2. Read Роль via `client.get_ticket_field_value(ticket_id, 24)`. If exactly `0` → add `FIELD_ROL: ROL_NE_VAZHNO`. If `None` or non-zero → skip Роль.
3. `env = await classify_environment(history)`. If truthy → `fields[FIELD_OKRUZHENIE] = env`. If `None` → send the topic warning message.
4. `await client.update_ticket_fields(ticket_id, fields)`.
5. All wrapped so a failure is logged, not raised.

Warning text: `⚠️ Окружение не определено автоматически — выставьте вручную`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_ticket_fields.py
import bot.ticket_fields as tf


@pytest.mark.asyncio
async def test_apply_env_found_role_empty(monkeypatch):
    sent = {}
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=0)
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value="11"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: касса POS не печатает")

    fake_client.update_ticket_fields.assert_awaited_once_with(
        "T1", {"3": "20", "24": "197", "2": "11"}
    )
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_apply_env_undetermined_warns(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=199)  # Роль set
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value=None))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: здравствуйте")

    # Klassifikaciya always; Роль skipped (was 199); Окружение skipped (None)
    fake_client.update_ticket_fields.assert_awaited_once_with("T1", {"3": "20"})
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["message_thread_id"] == 555
    assert "Окружение не определено" in kwargs["text"]


@pytest.mark.asyncio
async def test_apply_role_unknown_state_skipped(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=None)  # read error
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value="146"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await tf.apply_ticket_fields(bot, "T1", 555, "Эвотор завис")

    fake_client.update_ticket_fields.assert_awaited_once_with(
        "T1", {"3": "20", "2": "146"}
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ticket_fields.py -k apply -v`
Expected: FAIL — `AttributeError: module 'bot.ticket_fields' has no attribute 'apply_ticket_fields'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to bot/ticket_fields.py
from .config import config as _config  # already imported as `config`; reuse existing

ENV_UNDETERMINED_MSG = "⚠️ Окружение не определено автоматически — выставьте вручную"


async def apply_ticket_fields(bot, ticket_id: str, topic_id: int, history: str) -> None:
    """Auto-fill Классификация / Окружение / Роль after the AI summary.

    Never raises — any failure is logged so the summary flow is unaffected.
    """
    from .hde_api import HDEApiClient

    try:
        client = HDEApiClient()
    except Exception as exc:
        logger.warning("apply_ticket_fields: no HDE client: %s", exc)
        return

    fields: dict[str, str] = {FIELD_KLASSIFIKACIYA: KLASSIFIKACIYA_OBORUDOVANIE}

    # Роль: set only if currently empty (id == 0). None = read error → skip.
    try:
        current_rol = await client.get_ticket_field_value(ticket_id, int(FIELD_ROL))
    except Exception as exc:
        logger.warning("apply_ticket_fields: role read failed for %s: %s", ticket_id, exc)
        current_rol = None
    if current_rol == 0:
        fields[FIELD_ROL] = ROL_NE_VAZHNO

    # Окружение: LLM classify; overwrite always when determined.
    env_id = await classify_environment(history)
    if env_id:
        fields[FIELD_OKRUZHENIE] = env_id

    try:
        await client.update_ticket_fields(ticket_id, fields)
        logger.info("apply_ticket_fields: ticket %s updated %s", ticket_id, fields)
    except Exception as exc:
        logger.warning("apply_ticket_fields: update failed for %s: %s", ticket_id, exc)
        return

    if env_id is None:
        try:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=ENV_UNDETERMINED_MSG,
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning(
                "apply_ticket_fields: warn-message failed for topic %s: %s",
                topic_id, exc,
            )
```

Note: remove the redundant `from .config import config as _config` line if `config` is already imported at module top from Task 2 (it is — keep only the Task 2 import). Do not import `config` twice.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ticket_fields.py -k apply -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run full module test file**

Run: `python -m pytest tests/test_ticket_fields.py -v`
Expected: PASS (all tasks 1-4 tests green)

- [ ] **Step 6: Commit**

```bash
git add bot/ticket_fields.py tests/test_ticket_fields.py
git commit -m "feat(fields): add apply_ticket_fields orchestrator"
```

---

### Task 5: Hook into `_post_ticket_history`

**Files:**
- Modify: `bot/topic_manager.py` (after line 599, inside `_post_ticket_history`)
- Test: `tests/test_topic_manager.py`

The summary block ends at `topic_manager.py:599-601`:

```python
        await db.update_topic(ticket_id, ai_summary_sent_at=to_storage(utcnow()))
    except TelegramAPIError as exc:
        logger.warning("Failed to post AI summary to topic %d: %s", topic_id, exc)
```

Add the auto-fill call AFTER the existing try/except (own try/except so it never affects the summary). `plain_history` is already built at line 585 — but it is scoped inside the try; rebuild from `all_posts`/`info` to stay independent of the summary try block.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_topic_manager.py
from unittest.mock import patch as _patch


@pytest.mark.asyncio
async def test_post_ticket_history_triggers_field_autofill(monkeypatch):
    """After a successful AI summary, apply_ticket_fields must be invoked."""
    bot = make_bot()

    async def fake_summary(*a, **k):
        return ("суть", "ответ клиенту", "памятка", 80)

    monkeypatch.setattr(
        topic_manager, "_generate_summary_with_retry", AsyncMock(side_effect=fake_summary)
    )
    called = {}

    async def fake_apply(b, ticket_id, topic_id, history):
        called["args"] = (ticket_id, topic_id)

    with _patch("bot.ticket_fields.apply_ticket_fields", side_effect=fake_apply) as m:
        # Minimal posts/info doubles
        info = MagicMock()
        info.client_id = 1
        await topic_manager._post_ticket_history(
            bot, "TKT-9", 999, ticket_title="T", company_id="",
        )
    assert m.called
    assert called["args"] == ("TKT-9", 999)
```

If `_post_ticket_history` requires more setup (DB, HDE posts) than this double provides, follow the existing arrangement used by other `_post_ticket_history`-touching tests in this file (search for `_post_ticket_history`), reusing their fixtures/monkeypatches. The assertion that must hold: `bot.ticket_fields.apply_ticket_fields` is awaited once with `(ticket_id, topic_id)` after a non-None summary result.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_topic_manager.py -k autofill -v`
Expected: FAIL — `apply_ticket_fields` never called (assertion error / not patched target).

- [ ] **Step 3: Write minimal implementation**

In `bot/topic_manager.py`, locate the end of the summary `try/except` (lines 599-601). Immediately AFTER the `except TelegramAPIError` block (same indentation as the `try`), add:

```python
    try:
        from .ticket_fields import apply_ticket_fields
        from .ai_summary import _build_history_text as _bht
        _hist = _bht(all_posts, info)
        await apply_ticket_fields(bot, ticket_id, topic_id, _hist)
    except Exception as exc:
        logger.warning(
            "Ticket field auto-fill failed for %s: %s", ticket_id, exc
        )
```

Place this so it runs only when `result` is not None (it is after the `if result is None: return` guard at line 535-537, so any code after that guard already implies a summary was produced). Ensure the new block is at the function-body indentation level, after the summary try/except, not nested inside it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_topic_manager.py -k autofill -v`
Expected: PASS

- [ ] **Step 5: Run the broader topic_manager + fields suites**

Run: `python -m pytest tests/test_topic_manager.py tests/test_ticket_fields.py -v`
Expected: PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add bot/topic_manager.py tests/test_topic_manager.py
git commit -m "feat(topic): auto-fill ticket fields after AI summary"
```

---

### Task 6: Manual production verification (write-permission check)

**Files:** none (operational task)

HDE support confirmed the API key's staff account must have department access + field-edit permission. This must be verified against a real ticket before relying on the feature.

- [ ] **Step 1: Pick a safe live test ticket**

Choose a recent real ticket id (e.g. via the HDE UI) in the "Оборудование" department. Note it as `<TID>`.

- [ ] **Step 2: Dry-run the update against production**

Create a throwaway script `_tmp_verify.py` (delete after) that loads `.env` like the earlier scan scripts and calls:

```python
import asyncio
from bot.hde_api import HDEApiClient

async def main():
    c = HDEApiClient()
    before = await c.get_ticket_field_value("<TID>", 3)
    print("Классификация before:", before)
    res = await c.update_ticket_fields("<TID>", {"3": "20"})
    print("PUT status:", res.status)
    after = await c.get_ticket_field_value("<TID>", 3)
    print("Классификация after:", after)

asyncio.run(main())
```

Run: `python _tmp_verify.py`
Expected: `PUT status: 200` and `after: 20`. If status is 403 / value unchanged → the API account lacks field-edit permission; STOP and report (the staff account needs the permission per HDE support's note before this feature can work).

- [ ] **Step 3: Clean up**

```bash
rm -f _tmp_verify.py
```

- [ ] **Step 4: Deploy**

```bash
git push origin main
ssh config1 "cd /opt/hde-bot && git pull && sudo systemctl restart hde-bot.service"
```

Then trigger a real ticket flow and confirm in the HDE UI that Классификация = "Оборудование", Окружение filled (or the topic warning appeared), Роль = "Не важно" when it was previously empty.

---

## Self-Review

**Spec coverage:**
- Классификация always 20 → Task 4 (`fields` seeded with `FIELD_KLASSIFIKACIYA: KLASSIFIKACIYA_OBORUDOVANIE`), tested in all Task 4 tests. ✓
- Окружение LLM-classified, overwrite always → Task 2 (classifier) + Task 4 (added unconditionally when determined). ✓
- Окружение undetermined → topic warning, field untouched → Task 4 `test_apply_env_undetermined_warns`. ✓
- Роль = 197 only if empty → Task 3 (read helper) + Task 4 (`current_rol == 0` guard); `None`/non-zero skips. ✓
- Do not touch ИИ(11)/Вторая линия(8) → never added to `fields`. ✓
- Trigger after AI summary → Task 5 hook after summary block. ✓
- Field-update failure must not break summary → Task 5 own try/except + Task 4 never raises. ✓
- Write-permission constraint from HDE support → Task 6 manual verification. ✓

**Placeholder scan:** No TBD/TODO; all code blocks complete; the only conditional ("if more setup needed") in Task 5 Step 1 points to a concrete existing pattern in the same test file. Acceptable — `_post_ticket_history`'s full fixture set is environment-specific and must follow in-repo precedent.

**Type consistency:** `FIELD_*` constants are strings ("2"/"3"/"24"); `get_ticket_field_value` takes `int(FIELD_ROL)` and returns `int | None`; `classify_environment` returns `str | None` keyed into `OKRUZHENIE_OPTIONS` (str keys); `apply_ticket_fields(bot, ticket_id, topic_id, history)` signature consistent across Task 4 impl, Task 4 tests, and Task 5 call site. `update_ticket_fields(ticket_id, dict[str,str])` matches the existing method in `hde_api.py`. Consistent.
