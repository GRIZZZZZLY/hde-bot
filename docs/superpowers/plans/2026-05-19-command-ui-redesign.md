# Bot Command UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Declutter the bot's command surface: scope the Telegram command menu by chat type (DM-admin vs operator-group), replace the stale flat command list with an inline menu hub in DM, remove `/start`, collapse the `/aistatus` alias, and hide rarely-used AI-maintenance commands from the menu (still callable by typing).

**Architecture:** (A) `bot/main.py` registers commands via `set_my_commands` with `BotCommandScope` — one list for `BotCommandScopeAllPrivateChats` (admin), one for `BotCommandScopeChat(group_chat_id)` (operator/topic). The command→scope mapping is a pure function `build_command_scopes()` in a new `bot/command_menu.py` so it is unit-testable. (B) A new inline hub (`/menu`, `/help`) renders an `InlineKeyboardMarkup` with sections and submenus; callbacks (`menu:*`) invoke the SAME module-level service functions the slash handlers already use (mirroring the existing `cb_report_yesterday` callback pattern at `bot/handlers/commands.py:322`), so there is no behavioral duplication. Rare AI-maintenance commands are kept as working handlers but excluded from every scope and surfaced only as text in a "commands with arguments" help screen.

**Tech Stack:** Python 3.11+, aiogram 3.x (`BotCommand`, `BotCommandScopeAllPrivateChats`, `BotCommandScopeChat`, `InlineKeyboardMarkup`, `InlineKeyboardButton`, `CallbackQuery`), pytest + unittest.mock (AsyncMock).

---

## Decisions (baked in — review here)

- **Removed entirely:** `/start` (its hub role is replaced by `/menu`). The `cmd_start` handler is deleted.
- **Collapsed:** `/aistatus` is currently a thin alias of `/aimetrics` (`bot/handlers/commands.py:577-578`). The alias handler is deleted; `/aimetrics` remains the single AI-stats command.
- **Visible in DM menu (`BotCommandScopeAllPrivateChats`):** `menu`, `help`, `status`, `refresh`, `vacation`, `workon`, `report`, `digest`, `aisummary`.
- **Visible in operator group menu (`BotCommandScopeChat`, chat = `config.group_chat_id`):** `note`, `send`, `delete`, `autofill`, `help`.
- **Hidden from all menus, still callable by typing:** `aiknowledge`, `aimetrics`, `aiimport`, `aireindex`, `aibackfill`, `aianalyze`, `aioptimize`, `promptrollback`.
- **Inline hub buttons trigger only no-arg/default actions.** Argument variants (`/vacation 3d`, `/report YYYY-MM-DD`, `/aisummary on/off`, `/aiimport [N] [owner_id]`, etc.) stay as typed commands, listed in a "ℹ️ Команды с аргументами" help screen which becomes the single source of truth (replacing the stale flat `/help` text).
- DRY: menu callbacks call the same already-imported service functions as the slash handlers (`count_active_topics`, `refresh_topics`, `send_morning_digest`, vacation/workon helpers, aisummary toggle). No command-handler refactor.

---

## File Structure

- **Create** `bot/command_menu.py` — pure command/scope definitions + `build_command_scopes()` + the inline keyboard builders + the actualized help/args text. Single responsibility: command-menu UI data.
- **Modify** `bot/main.py` — replace the flat `set_my_commands([...])` call (lines 76-96) with scoped registration using `build_command_scopes()`.
- **Modify** `bot/handlers/commands.py` — delete `cmd_start` (55-62) and `cmd_aistatus` (577-578); rewrite `cmd_help` to render the hub; add `cmd_menu` + `menu:*` callback handlers.
- **Create** `tests/test_command_menu.py` — unit tests for `build_command_scopes()` and the keyboard builders.
- **Modify** `tests/` (only if an existing test references `/start` or `/aistatus`).

---

### Task 1: command_menu module — scope definitions (pure data)

**Files:**
- Create: `bot/command_menu.py`
- Test: `tests/test_command_menu.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_command_menu.py
from bot.command_menu import (
    DM_COMMANDS,
    GROUP_COMMANDS,
    HIDDEN_COMMANDS,
    build_command_scopes,
)


def test_command_sets_partitioned():
    dm = {c.command for c in DM_COMMANDS}
    grp = {c.command for c in GROUP_COMMANDS}
    assert dm == {
        "menu", "help", "status", "refresh", "vacation",
        "workon", "report", "digest", "aisummary",
    }
    assert grp == {"note", "send", "delete", "autofill", "help"}
    # Removed commands appear nowhere
    assert "start" not in dm | grp | set(HIDDEN_COMMANDS)
    assert "aistatus" not in dm | grp | set(HIDDEN_COMMANDS)
    # Hidden = still-callable maintenance, never in a visible menu
    assert set(HIDDEN_COMMANDS) == {
        "aiknowledge", "aimetrics", "aiimport", "aireindex",
        "aibackfill", "aianalyze", "aioptimize", "promptrollback",
    }
    assert (dm | grp) & set(HIDDEN_COMMANDS) == set()


def test_build_command_scopes_shape():
    scopes = build_command_scopes(group_chat_id=-100123)
    kinds = {s["scope"].type for s in scopes}
    assert "all_private_chats" in kinds
    assert "chat" in kinds
    for entry in scopes:
        assert entry["commands"]  # non-empty
        assert all(hasattr(c, "command") for c in entry["commands"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_command_menu.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.command_menu'`

- [ ] **Step 3: Write minimal implementation**

```python
# bot/command_menu.py
"""Command-menu UI: scoped command lists + inline hub keyboards.

Single source of truth for which slash-commands are advertised where.
Rare AI-maintenance commands stay callable by typing but are listed in
HIDDEN_COMMANDS and never registered in a Telegram command menu.
"""
from __future__ import annotations

from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

# DM / admin menu (private chats)
DM_COMMANDS: list[BotCommand] = [
    BotCommand(command="menu",     description="Меню бота"),
    BotCommand(command="help",     description="Меню и справка"),
    BotCommand(command="status",   description="Активные топики и pre-SLA"),
    BotCommand(command="refresh",  description="Синхронизировать топики с HDE"),
    BotCommand(command="vacation", description="Режим тишины (напр. /vacation 3d)"),
    BotCommand(command="workon",   description="Снять режим тишины"),
    BotCommand(command="report",   description="Отчёт в Google Sheets (за вчера)"),
    BotCommand(command="digest",   description="Вызвать утреннюю сводку"),
    BotCommand(command="aisummary", description="Вкл/выкл AI саммари тикета"),
]

# Operator group / topic menu
GROUP_COMMANDS: list[BotCommand] = [
    BotCommand(command="note",     description="Внутренний комментарий в HDE"),
    BotCommand(command="send",     description="Публичный ответ клиенту через HDE"),
    BotCommand(command="delete",   description="Удалить сообщение из HDE"),
    BotCommand(command="autofill", description="Заполнить Окружение/Классификацию/Роль"),
    BotCommand(command="help",     description="Меню и справка"),
]

# Callable by typing, never shown in a menu
HIDDEN_COMMANDS: list[str] = [
    "aiknowledge", "aimetrics", "aiimport", "aireindex",
    "aibackfill", "aianalyze", "aioptimize", "promptrollback",
]


def build_command_scopes(group_chat_id: int) -> list[dict]:
    """Return [{scope, commands}] for bot.set_my_commands per scope."""
    return [
        {
            "scope": BotCommandScopeAllPrivateChats(),
            "commands": DM_COMMANDS,
        },
        {
            "scope": BotCommandScopeChat(chat_id=group_chat_id),
            "commands": GROUP_COMMANDS,
        },
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_command_menu.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/command_menu.py tests/test_command_menu.py
git commit -m "feat(menu): scoped command-set definitions"
```

---

### Task 2: inline hub keyboards + args-help text (pure builders)

**Files:**
- Modify: `bot/command_menu.py`
- Test: `tests/test_command_menu.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/test_command_menu.py
from bot.command_menu import hub_keyboard, submenu_keyboard, ARGS_HELP_TEXT


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_hub_keyboard_root():
    kb = hub_keyboard()
    cbs = _callbacks(kb)
    assert "menu:status" in cbs
    assert "menu:refresh" in cbs
    assert "menu:quiet" in cbs       # submenu
    assert "menu:reports" in cbs     # submenu
    assert "menu:ai" in cbs          # submenu
    assert "menu:args" in cbs        # args help


def test_submenu_keyboards_have_back():
    for name in ("quiet", "reports", "ai"):
        kb = submenu_keyboard(name)
        cbs = _callbacks(kb)
        assert "menu:root" in cbs    # back button
        assert cbs, f"{name} submenu empty"


def test_submenu_actions():
    assert "menu:vacation" in _callbacks(submenu_keyboard("quiet"))
    assert "menu:workon" in _callbacks(submenu_keyboard("quiet"))
    assert "menu:report_yesterday" in _callbacks(submenu_keyboard("reports"))
    assert "menu:digest" in _callbacks(submenu_keyboard("reports"))
    assert "menu:aisummary" in _callbacks(submenu_keyboard("ai"))
    assert "menu:aimetrics" in _callbacks(submenu_keyboard("ai"))


def test_args_help_lists_arg_variants():
    for frag in ("/vacation 3d", "/report ", "/aisummary on", "/aiimport"):
        assert frag in ARGS_HELP_TEXT


def test_unknown_submenu_raises():
    import pytest
    with pytest.raises(KeyError):
        submenu_keyboard("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_command_menu.py -k "hub or submenu or args" -v`
Expected: FAIL — `ImportError: cannot import name 'hub_keyboard'`

- [ ] **Step 3: Write minimal implementation (append to `bot/command_menu.py`)**

```python
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


def hub_keyboard() -> InlineKeyboardMarkup:
    return _kb([
        [("📊 Статус", "menu:status"), ("🔄 Синхрон", "menu:refresh")],
        [("💤 Тишина ▸", "menu:quiet"), ("📈 Отчёты ▸", "menu:reports")],
        [("🤖 AI ▸", "menu:ai")],
        [("ℹ️ Команды с аргументами", "menu:args")],
    ])


_SUBMENUS: dict[str, list[list[tuple[str, str]]]] = {
    "quiet": [
        [("💤 Тишина до след. раб. дня", "menu:vacation")],
        [("▶️ Снять тишину", "menu:workon")],
        [("⬅️ Назад", "menu:root")],
    ],
    "reports": [
        [("📈 Отчёт за вчера", "menu:report_yesterday")],
        [("🌅 Утренняя сводка", "menu:digest")],
        [("⬅️ Назад", "menu:root")],
    ],
    "ai": [
        [("🧠 AI summary вкл/выкл", "menu:aisummary")],
        [("📚 База знаний", "menu:aimetrics")],
        [("⬅️ Назад", "menu:root")],
    ],
}


def submenu_keyboard(name: str) -> InlineKeyboardMarkup:
    return _kb(_SUBMENUS[name])  # KeyError on unknown name (caller guards)


HUB_TITLE = "🤖 <b>Меню бота</b>\nВыберите действие:"

ARGS_HELP_TEXT = (
    "ℹ️ <b>Команды с аргументами</b>\n\n"
    "<b>Тишина</b>\n"
    "/vacation 3d — режим тишины на N дней\n"
    "/vacation 2026-05-30 — тишина до даты\n\n"
    "<b>Отчёты</b>\n"
    "/report 2026-05-18 — отчёт за конкретную дату\n\n"
    "<b>AI</b>\n"
    "/aisummary on | /aisummary off — вкл/выкл AI-саммари\n"
    "/aiknowledge — статистика базы знаний\n"
    "/aimetrics — управление базой знаний\n"
    "/aiimport [N] [owner_id] — импорт закрытых тикетов HDE\n"
    "/aireindex — переиндексировать embeddings\n"
    "/aibackfill — дозаполнить организации\n"
    "/aianalyze — извлечь паттерны решений\n"
    "/aioptimize — ручная оптимизация промпта\n"
    "/promptrollback — откатить версию промпта\n\n"
    "<b>В топике тикета</b>\n"
    "/note текст | /send текст | /delete (в ответ) | /autofill"
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_command_menu.py -v`
Expected: PASS (all Task 1 + Task 2 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/command_menu.py tests/test_command_menu.py
git commit -m "feat(menu): inline hub + submenu keyboards + args help"
```

---

### Task 3: wire scopes in main.py + replace /help, /menu, callbacks; remove /start, /aistatus

**Files:**
- Modify: `bot/main.py` (replace `set_my_commands` block at lines 76-96)
- Modify: `bot/handlers/commands.py` (delete `cmd_start` 55-62; delete `cmd_aistatus` 577-578; rewrite `cmd_help`; add `cmd_menu` + `menu:*` callbacks)
- Test: `tests/test_command_menu.py` (callback dispatch test)

- [ ] **Step 1: Write the failing test (append to `tests/test_command_menu.py`)**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_menu_callback_status(monkeypatch):
    import bot.handlers.commands as cmds

    monkeypatch.setattr(cmds, "count_active_topics", AsyncMock(return_value=3))
    monkeypatch.setattr(cmds, "count_pending_delete_topics", AsyncMock(return_value=1))
    monkeypatch.setattr(cmds, "count_pending_pre_sla_topics", AsyncMock(return_value=2))
    monkeypatch.setattr(cmds, "count_total_topics", AsyncMock(return_value=9))

    cb = MagicMock()
    cb.data = "menu:status"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()

    await cmds.cb_menu(cb)

    cb.answer.assert_awaited()
    # status text rendered with the mocked counts
    sent = " ".join(
        str(c.args[0]) for c in cb.message.edit_text.await_args_list
        + cb.message.answer.await_args_list
    )
    assert "Активных topics" in sent and "3" in sent


@pytest.mark.asyncio
async def test_menu_callback_navigation(monkeypatch):
    import bot.handlers.commands as cmds
    cb = MagicMock()
    cb.data = "menu:quiet"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    await cmds.cb_menu(cb)
    cb.message.edit_text.assert_awaited()  # navigated into submenu
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_command_menu.py -k menu_callback -v`
Expected: FAIL — `AttributeError: module 'bot.handlers.commands' has no attribute 'cb_menu'`

- [ ] **Step 3a: main.py — scoped registration**

In `bot/main.py`, add to imports near line 6-7:

```python
from aiogram.types import BotCommand, ErrorEvent, Update  # existing
from .command_menu import build_command_scopes
```

Replace the entire `await bot.set_my_commands([...])` block (lines 76-96, the call with the flat list ending `])`) with:

```python
    await bot.delete_my_commands()
    for entry in build_command_scopes(config.group_chat_id):
        await bot.set_my_commands(entry["commands"], scope=entry["scope"])
```

(`config` is already imported in main.py. `delete_my_commands()` clears the old global flat list so removed commands like `/start` disappear from existing clients.)

- [ ] **Step 3b: commands.py — delete removed handlers**

Delete the `cmd_start` handler block (the `@router.message(Command("start"))` decorator + function, currently `bot/handlers/commands.py:55-62`).

Delete the `cmd_aistatus` handler block (`@router.message(Command("aistatus"))` + its 2-line body, currently `bot/handlers/commands.py:577-578`). First open it to confirm it is only `return await cmd_aimetrics(message)` (a pure alias); if it contains unique logic, STOP and report — do not delete logic.

- [ ] **Step 3c: commands.py — add /menu, rewrite /help, add callback router**

Add import at top of `bot/handlers/commands.py` (with the other `from ..` imports):

```python
from ..command_menu import hub_keyboard, submenu_keyboard, HUB_TITLE, ARGS_HELP_TEXT
```

Replace the whole `cmd_help` function (`@router.message(Command("help"))` + body, currently starts `bot/handlers/commands.py:82`) with:

```python
@router.message(Command("help"))
@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(HUB_TITLE, parse_mode="HTML", reply_markup=hub_keyboard())
```

Add a single callback handler (place it next to the other `@router.callback_query` handlers, e.g. after `cb_report_yesterday` which ends near `bot/handlers/commands.py:355`):

```python
@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery) -> None:
    action = callback.data.split(":", 1)[1]
    await callback.answer()

    # ── navigation ──────────────────────────────────────────────
    if action == "root":
        await callback.message.edit_text(
            HUB_TITLE, parse_mode="HTML", reply_markup=hub_keyboard()
        )
        return
    if action in ("quiet", "reports", "ai"):
        await callback.message.edit_text(
            HUB_TITLE, parse_mode="HTML", reply_markup=submenu_keyboard(action)
        )
        return
    if action == "args":
        await callback.message.edit_text(
            ARGS_HELP_TEXT,
            parse_mode="HTML",
            reply_markup=submenu_keyboard("quiet").__class__(
                inline_keyboard=[[
                    __import__("aiogram").types.InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="menu:root"
                    )
                ]]
            ),
        )
        return

    # ── actions (reuse the same service fns as the slash handlers) ─
    if action == "status":
        active = await count_active_topics()
        pending_delete = await count_pending_delete_topics()
        pending_pre_sla = await count_pending_pre_sla_topics()
        total = await count_total_topics()
        await callback.message.answer(
            "📊 <b>Статус бота</b>\n\n"
            f"🟢 Активных topics: <b>{active}</b>\n"
            f"⏳ Ожидают удаления: <b>{pending_delete}</b>\n"
            f"⏰ Ожидают pre-SLA: <b>{pending_pre_sla}</b>\n"
            f"📚 Всего записей: <b>{total}</b>",
            parse_mode="HTML",
        )
        return
    if action == "refresh":
        wait = await callback.message.answer("🔄 Синхронизирую с HDE...")
        try:
            result = await refresh_topics(bot=callback.bot)
            text = format_refresh_result(result)
        except Exception as exc:  # noqa: BLE001
            text = f"⚠️ <b>Ошибка:</b> {exc}"
        try:
            await wait.delete()
        except Exception:
            pass
        await callback.message.answer(text, parse_mode="HTML")
        return
    if action == "report_yesterday":
        await cb_report_yesterday(callback)  # existing callback, full flow
        return
    if action == "digest":
        try:
            await send_morning_digest(callback.bot)
            await callback.message.answer("🌅 Утренняя сводка отправлена.")
        except Exception as exc:  # noqa: BLE001
            await callback.message.answer(f"⚠️ <b>Ошибка:</b> {exc}", parse_mode="HTML")
        return
    if action == "vacation":
        from ..work_schedule import next_work_start, set_vacation
        until = next_work_start()
        set_vacation(until)
        await callback.message.answer(
            "💤 <b>Режим тишины включён</b> до следующего рабочего дня.",
            parse_mode="HTML",
        )
        return
    if action == "workon":
        from ..work_schedule import is_on_vacation, set_vacation
        if not is_on_vacation():
            await callback.message.answer("ℹ️ Режим тишины не активен.")
            return
        set_vacation(None)
        await callback.message.answer(
            "✅ <b>Режим тишины отключён.</b>", parse_mode="HTML"
        )
        return
    if action == "aisummary":
        from .. import db as _db
        cur = await _db.get_bot_setting("ai_summary_enabled", "1")
        new = "0" if cur == "1" else "1"
        await _db.set_bot_setting("ai_summary_enabled", new)
        await callback.message.answer(
            "🧠 <b>AI Саммари включён</b> ✅" if new == "1"
            else "🧠 <b>AI Саммари выключен</b> ❌",
            parse_mode="HTML",
        )
        return
    if action == "aimetrics":
        await cmd_aimetrics(callback.message)
        return
```

Before writing 3c, the implementer MUST verify the exact service-function names/signatures by reading the current bodies of `cmd_status`, `cmd_refresh`, `cmd_digest`, `cmd_vacation`, `cmd_workon`, `cmd_aisummary`, `cmd_aimetrics` in `bot/handlers/commands.py`, and the bot-setting accessor names in `bot/db.py` (`get_bot_setting`/`set_bot_setting` or equivalent — match what `cmd_aisummary` actually uses). If a name differs, use the real one and keep the callback behavior identical to the corresponding slash command. The `args` back-button construction above is intentionally explicit; if the implementer prefers, replace it with a tiny module-level `back_keyboard()` added to `bot/command_menu.py` (and a matching test) — that is the cleaner option and is permitted.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_command_menu.py -v`
Expected: PASS. Then `python -m pytest tests/ -q` — only the known pre-existing `test_scheduler_sends_pre_sla_alert` may fail; no new failures. Then `python -c "import bot.main, bot.handlers.commands, bot.command_menu"` exits 0.

- [ ] **Step 5: Commit**

```bash
git add bot/main.py bot/handlers/commands.py bot/command_menu.py tests/test_command_menu.py
git commit -m "feat(menu): scoped menus + inline hub; drop /start and /aistatus alias"
```

---

### Task 4: manual verification + deploy

**Files:** none (operational)

- [ ] **Step 1: Local smoke**

Run `python -c "import bot.main"` and `python -m pytest tests/test_command_menu.py tests/test_topic_manager.py tests/test_ticket_fields.py -q`. Confirm only the pre-existing scheduler test fails.

- [ ] **Step 2: Deploy**

```bash
git push origin main
ssh config1 "cd /opt/hde-bot && git pull --ff-only && sudo systemctl restart hde-bot.service && sleep 3 && systemctl is-active hde-bot.service"
```

- [ ] **Step 3: Verify in Telegram**

In DM: command menu shows only the DM set; `/menu` and `/help` open the inline hub; submenu navigation (Тишина/Отчёты/AI), back button, and "Команды с аргументами" work; Status/Refresh/Отчёт за вчера/Дайджест/Тишина/Снять/AI toggle produce the same result as the old slash commands. In the operator group: command menu shows only note/send/delete/autofill/help. Confirm `/start` no longer appears; typing `/aistatus` no longer responds while `/aimetrics` still works; hidden commands (`/aiimport` etc.) still work when typed.

---

## Self-Review

**Spec coverage:**
- A — scoped Telegram menus (DM vs group): Task 1 (`build_command_scopes`) + Task 3a (main.py wiring). ✓
- B — inline hub in DM replacing flat list: Task 2 (keyboards/args text) + Task 3c (`/menu`,`/help`→hub, `cb_menu`). ✓
- Remove `/start`: Task 3b. ✓
- Collapse `/aistatus` alias: Task 3b (with safety check before delete). ✓
- Hide rare AI-maintenance but keep callable: `HIDDEN_COMMANDS` (Task 1) excluded from scopes; handlers untouched; surfaced in `ARGS_HELP_TEXT` (Task 2). ✓
- Actualize the stale list: `ARGS_HELP_TEXT` is the single maintained reference; old flat `cmd_help` text deleted (Task 3c). ✓
- Topic-only vs DM-only split: GROUP_COMMANDS vs DM_COMMANDS. ✓

**Placeholder scan:** No TBD/TODO. The one explicit "verify real service-fn names before writing 3c" instruction names the exact functions/files to check and the required invariant (behavior identical to slash command) — concrete, not a placeholder; necessary because callback bodies must call real existing functions. The `args` back-button has concrete code plus an optional cleaner refactor that itself is fully specified (add `back_keyboard()` + test).

**Type consistency:** `build_command_scopes` returns `list[dict]` with `{"scope", "commands"}` — consumed identically in Task 1 test and Task 3a. `hub_keyboard()`/`submenu_keyboard(name)` return `InlineKeyboardMarkup`; callbacks use `callback_data` strings `menu:<action>` consistently across keyboards (Task 2) and `cb_menu` dispatch (Task 3c): root/quiet/reports/ai/args/status/refresh/report_yesterday/digest/vacation/workon/aisummary/aimetrics — every callback_data produced by a keyboard has a matching branch, and every branch's data is produced by some keyboard. `HUB_TITLE`/`ARGS_HELP_TEXT` defined Task 2, used Task 3c.
