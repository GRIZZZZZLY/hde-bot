# Inline Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every bot action is reachable by inline buttons — a full menu hub in DMs/groups and "write, then pick where it goes" in ticket topics — while every old command keeps working when typed.

**Architecture:** Stage 1 rebuilds the existing hub (`bot/command_menu.py` keyboards + `cb_menu` in `bot/handlers/commands.py`) so argument variants become buttons, and extracts the shared bodies of `/status`, `/refresh`, `/vacation`, `/report`, `/weekly`, `/aioptimize`, `/promptrollback` so commands and buttons run the same code. Stage 2 adds `bot/handlers/topic_actions.py`: the bot replies to an engineer's topic message with «Куда отправить?», and the button press finds the original message in `callback.message.reply_to_message`, so nothing is stored; sending goes through the existing `send_public_reply` / `add_internal_note`.

**Tech Stack:** Python 3.13, aiogram 3.26, pytest (`asyncio_mode = auto`), SQLite via `bot.db`.

**Spec:** `docs/superpowers/specs/2026-10-12-inline-navigation-design.md`

## Global Constraints

- Old commands stay registered and behave as before when typed (`/send`, `/note`, `/delete`, `/autofill`, `/vacation 3d`, `/report YYYY-MM-DD`, `/weekly YYYY-MM-DD`, `/taketimes …`, all AI commands).
- Advertised command lists: `DM_COMMANDS = [menu]`, `GROUP_COMMANDS = [menu]`; `start` is in no list.
- `callback_data` ≤ 64 bytes; menu data starts `menu:`, topic data starts `tp:`.
- UI copy in Russian; messages use `parse_mode="HTML"`; never echo user text into HTML without `html.escape`.
- One menu for all three engineers (owner decision). Existing permission guards stay: `/aioptimize` and `/promptrollback` only for `config.operator_telegram_user_ids`.
- Topic router is included into `commands.router` after the ai/optimizer/report routers and **before** `_media_router`.
- Live check of stage 2 uses an internal comment only; no public test message to a client without separate owner consent.
- Prod log: `/opt/hde-bot/logs/bot.log` (journalctl is broken on config1).

## Review Focus

1. Two fast taps on «📤 Клиенту» → exactly one post to HDE (in-flight guard) — test in Task 5.
2. Someone who is not an engineer presses «📤 Клиенту» → nothing sent, error shown, buttons back — test in Task 5.
3. Engineer writes in the topic of an inactive/deleted ticket (or in General) → no question at all — test in Task 5.
4. An audio file (call recording) or voice note in a topic → no question, and the update still reaches the transcription handlers — test in Task 5 (filter + router order).
5. `take_busy_hours` already holds an option outside the grid (e.g. `5`, set by `/taketimes`) → the grid shows it and toggling another option keeps it — test in Task 1.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `bot/command_menu.py` | Command lists, every menu keyboard, menu texts, pure helpers (dates, hour toggle) | Rewrite keyboards/texts |
| `bot/handlers/commands.py` | `/start`/`/menu`, `cb_menu` screens and actions, shared status/refresh/vacation/autofill bodies | Modify |
| `bot/handlers/report_commands.py` | `/report`, `/weekly` bodies as `run_daily_report` / `run_weekly` | Modify |
| `bot/handlers/optimizer_commands.py` | `/aioptimize`, `/promptrollback` bodies as `run_aioptimize` / `show_promptrollback` | Modify |
| `bot/handlers/topic_actions.py` | «Куда отправить?» question, send/delete/autofill callbacks | Create |
| `bot/handlers/ai_feedback.py` | `suggest_button_kb` gets 🪄 | Modify |
| `tests/test_command_menu.py` | Stage 1 tests | Rewrite |
| `tests/test_topic_actions.py` | Stage 2 tests | Create |

---

### Task 1: Menu keyboards and texts (pure)

**Order:** run Task 2 first (it commits on its own), then Task 1, then Task 3 (Task 1 and 3 share one commit).

**Files:**
- Modify: `bot/command_menu.py` (whole file)
- Test: `tests/test_command_menu.py` (replace the pure-keyboard tests; keep the two async `cb_menu` tests for Task 3)

**Interfaces:**
- Produces (all in `bot.command_menu`):
  - `DM_COMMANDS: list[BotCommand]`, `GROUP_COMMANDS: list[BotCommand]`, `HIDDEN_COMMANDS: list[str]`, `build_command_scopes(chat_ids: list[int]) -> list[dict]` (unchanged signature)
  - `MENU_BUTTON_TEXT: str = "☰ Меню"`, `menu_reply_keyboard() -> ReplyKeyboardMarkup`
  - `HUB_TITLE: str`, `HOWTO_TEXT: str`
  - `hub_keyboard()`, `quiet_keyboard(active: bool)`, `reports_keyboard()`, `days_keyboard(today: date, work_days)`, `weeks_keyboard(today: date)`, `ai_keyboard(summary_on: bool)`, `maint_keyboard()`, `confirm_keyboard(cmd: str)`, `hours_keyboard(selected: list[str])`, `back_keyboard()` — all `-> InlineKeyboardMarkup`
  - `MAINT_COMMANDS: dict[str, str]` (command → button label)
  - `HOUR_GRID: tuple[str, ...]`
  - `recent_work_days(today: date, work_days, count: int = 7) -> list[date]`
  - `last_sundays(today: date, count: int = 3) -> list[date]`
  - `toggle_hour(selected: list[str], option: str) -> list[str] | None`
- Removed: `submenu_keyboard`, `ARGS_HELP_TEXT` (callers fixed in Task 3).

- [ ] **Step 1: Write the failing tests**

Replace everything in `tests/test_command_menu.py` above `test_menu_callback_status` with:

```python
from datetime import date

import pytest

from bot.command_menu import (
    DM_COMMANDS,
    GROUP_COMMANDS,
    HIDDEN_COMMANDS,
    HOUR_GRID,
    MAINT_COMMANDS,
    build_command_scopes,
    confirm_keyboard,
    days_keyboard,
    hours_keyboard,
    hub_keyboard,
    last_sundays,
    maint_keyboard,
    menu_reply_keyboard,
    quiet_keyboard,
    recent_work_days,
    reports_keyboard,
    toggle_hour,
    weeks_keyboard,
    ai_keyboard,
)


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_only_menu_is_advertised():
    assert [c.command for c in DM_COMMANDS] == ["menu"]
    assert [c.command for c in GROUP_COMMANDS] == ["menu"]
    assert "start" not in set(HIDDEN_COMMANDS)
    assert set(HIDDEN_COMMANDS) == {
        "aiknowledge", "aimetrics", "aiimport", "aireindex",
        "aibackfill", "aianalyze", "aioptimize", "promptrollback",
    }


def test_build_command_scopes_shape():
    scopes = build_command_scopes([-100123])
    kinds = {s["scope"].type for s in scopes}
    assert kinds == {"all_private_chats", "chat"}


def test_menu_reply_keyboard_is_persistent():
    kb = menu_reply_keyboard()
    assert kb.keyboard[0][0].text == "☰ Меню"
    assert kb.is_persistent and kb.resize_keyboard


def test_hub_has_every_screen():
    cbs = _callbacks(hub_keyboard())
    for data in ("menu:status", "menu:refresh", "menu:quiet", "menu:reports",
                 "menu:ai", "menu:hours", "menu:howto"):
        assert data in cbs
    assert "menu:args" not in cbs


def test_quiet_keyboard_shows_workon_only_when_active():
    assert "menu:workon" not in _callbacks(quiet_keyboard(active=False))
    cbs = _callbacks(quiet_keyboard(active=True))
    assert {"menu:vac:next", "menu:vac:1", "menu:vac:3", "menu:vac:7", "menu:workon", "menu:root"} <= set(cbs)


def test_reports_keyboard():
    cbs = _callbacks(reports_keyboard())
    assert {"menu:report_yesterday", "menu:days", "menu:weeks", "menu:digest", "menu:root"} <= set(cbs)


def test_recent_work_days_skips_days_off_and_today():
    # 2026-10-13 is a Tuesday; work days Mon-Fri
    days = recent_work_days(date(2026, 10, 13), (0, 1, 2, 3, 4))
    assert days[0] == date(2026, 10, 12)          # Monday, never today
    assert date(2026, 10, 11) not in days          # Sunday skipped
    assert len(days) == 7
    assert all(d.weekday() < 5 for d in days)


def test_days_keyboard_labels_and_data():
    kb = days_keyboard(date(2026, 10, 13), (0, 1, 2, 3, 4))
    assert "menu:rep:2026-10-12" in _callbacks(kb)
    assert "пн 12.10" in _texts(kb)
    assert "menu:reports" in _callbacks(kb)


def test_last_sundays():
    assert last_sundays(date(2026, 10, 13)) == [date(2026, 10, 11), date(2026, 10, 4), date(2026, 9, 27)]
    assert last_sundays(date(2026, 10, 11))[0] == date(2026, 10, 11)   # Sunday itself


def test_weeks_keyboard():
    cbs = _callbacks(weeks_keyboard(date(2026, 10, 13)))
    assert cbs[:3] == ["menu:week:2026-10-11", "menu:week:2026-10-04", "menu:week:2026-09-27"]


def test_ai_keyboard_label_shows_state():
    assert "вкл" in _texts(ai_keyboard(True))[0]
    assert "выкл" in _texts(ai_keyboard(False))[0]
    assert {"menu:aisummary", "menu:aimetrics", "menu:aiknowledge", "menu:maint"} <= set(_callbacks(ai_keyboard(True)))


def test_maint_and_confirm():
    cbs = _callbacks(maint_keyboard())
    assert [f"menu:ask:{c}" for c in MAINT_COMMANDS] == cbs[:-1]
    assert _callbacks(confirm_keyboard("aiimport")) == ["menu:run:aiimport", "menu:maint"]
    assert all(len(d.encode()) <= 64 for d in cbs)


def test_hours_keyboard_marks_selected():
    texts = _texts(hours_keyboard(["1", "1-2"]))
    assert "✅ 1 ч" in texts and "✅ 1–2 ч" in texts and "4 ч" in texts


def test_hours_keyboard_keeps_custom_option():
    kb = hours_keyboard(["5", "1"])
    assert "menu:hour:5" in _callbacks(kb)
    assert "✅ 5 ч" in _texts(kb)


@pytest.mark.parametrize("selected,option,expected", [
    (["1", "4"], "2", ["1", "2", "4"]),          # added in grid order
    (["1", "4"], "4", ["1"]),                    # removed
    (["1"], "1", None),                          # cannot drop the last one
    (["1", "1-2", "2", "3", "4", "6"], "8", None),  # cannot pick a 7th
    (["5", "1"], "2", ["1", "2", "5"]),          # custom option kept, after the grid
])
def test_toggle_hour(selected, option, expected):
    assert toggle_hour(selected, option) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_command_menu.py -q`
Expected: FAIL — `ImportError: cannot import name 'HOUR_GRID'`.

- [ ] **Step 3: Write the implementation**

Replace `bot/command_menu.py` with:

```python
"""Command-menu UI: scoped command lists, every menu keyboard and menu texts.

Single source of truth for which slash-commands are advertised and for every
menu screen. All actions live behind buttons, so only /menu is advertised;
the old commands still work when typed.
"""
from __future__ import annotations

from datetime import date, timedelta

from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

DM_COMMANDS: list[BotCommand] = [BotCommand(command="menu", description="Меню бота")]
GROUP_COMMANDS: list[BotCommand] = [BotCommand(command="menu", description="Меню бота")]

# Callable by typing and from 🛠 Обслуживание, never shown in a command menu
HIDDEN_COMMANDS: list[str] = [
    "aiknowledge", "aimetrics", "aiimport", "aireindex",
    "aibackfill", "aianalyze", "aioptimize", "promptrollback",
]


def build_command_scopes(chat_ids: list[int]) -> list[dict]:
    """Return [{scope, commands}] for bot.set_my_commands: private chats + every engineer's group."""
    return [
        {
            "scope": BotCommandScopeAllPrivateChats(),
            "commands": DM_COMMANDS,
        },
        *(
            {"scope": BotCommandScopeChat(chat_id=chat_id), "commands": GROUP_COMMANDS}
            for chat_id in chat_ids
        ),
    ]


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

MENU_BUTTON_TEXT = "☰ Меню"


def menu_reply_keyboard() -> ReplyKeyboardMarkup:
    """The ☰ button that stays under the input field in a private chat."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


_BACK = [("⬅️ Назад", "menu:root")]


def hub_keyboard() -> InlineKeyboardMarkup:
    return _kb([
        [("📊 Статус", "menu:status"), ("🔄 Синхрон", "menu:refresh")],
        [("💤 Тишина ▸", "menu:quiet"), ("📈 Отчёты ▸", "menu:reports")],
        [("🤖 AI ▸", "menu:ai"), ("⏳ Часы «Беру» ▸", "menu:hours")],
        [("❓ Как отвечать из топика", "menu:howto")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return _kb([_BACK])


def quiet_keyboard(active: bool) -> InlineKeyboardMarkup:
    rows = [
        [("До след. раб. дня", "menu:vac:next")],
        [("1 д", "menu:vac:1"), ("3 д", "menu:vac:3"), ("7 д", "menu:vac:7")],
    ]
    if active:
        rows.append([("▶️ Снять тишину", "menu:workon")])
    rows.append(_BACK)
    return _kb(rows)


def reports_keyboard() -> InlineKeyboardMarkup:
    return _kb([
        [("📈 Отчёт за прошлый раб. день", "menu:report_yesterday")],
        [("📅 Другой день ▸", "menu:days"), ("🗓 Неделя ▸", "menu:weeks")],
        [("🌅 Утренняя сводка", "menu:digest")],
        _BACK,
    ])


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def recent_work_days(today: date, work_days, count: int = 7) -> list[date]:
    """The last `count` work days before `today` (today itself excluded), newest first."""
    days: list[date] = []
    day = today - timedelta(days=1)
    while len(days) < count and day > today - timedelta(days=31):
        if day.weekday() in work_days:
            days.append(day)
        day -= timedelta(days=1)
    return days


def days_keyboard(today: date, work_days) -> InlineKeyboardMarkup:
    buttons = [
        (f"{_WEEKDAYS[d.weekday()]} {d:%d.%m}", f"menu:rep:{d.isoformat()}")
        for d in recent_work_days(today, work_days)
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([("⬅️ Назад", "menu:reports")])
    return _kb(rows)


def last_sundays(today: date, count: int = 3) -> list[date]:
    """Most recent Sunday on or before `today`, then the ones before it.

    run_weekly_summary(sunday) covers sunday-7 .. sunday-3, i.e. the work week
    that ended before that Sunday.
    """
    last = today - timedelta(days=(today.weekday() + 1) % 7)
    return [last - timedelta(weeks=i) for i in range(count)]


_WEEK_LABELS = ("Прошлая", "Позапрошлая", "3 недели назад")


def weeks_keyboard(today: date) -> InlineKeyboardMarkup:
    rows = [
        [(f"{label}: {s - timedelta(days=7):%d.%m}–{s - timedelta(days=3):%d.%m}",
          f"menu:week:{s.isoformat()}")]
        for label, s in zip(_WEEK_LABELS, last_sundays(today))
    ]
    rows.append([("⬅️ Назад", "menu:reports")])
    return _kb(rows)


def ai_keyboard(summary_on: bool) -> InlineKeyboardMarkup:
    return _kb([
        [(f"🧠 Саммари: {'вкл ✅' if summary_on else 'выкл ❌'}", "menu:aisummary")],
        [("📚 База знаний", "menu:aimetrics"), ("📊 Статистика", "menu:aiknowledge")],
        [("🛠 Обслуживание ▸", "menu:maint")],
        _BACK,
    ])


MAINT_COMMANDS: dict[str, str] = {
    "aiimport": "📥 Импорт тикетов",
    "aireindex": "🔁 Переиндексация",
    "aibackfill": "🏢 Дозаполнить организации",
    "aianalyze": "🔍 Анализ паттернов",
    "aioptimize": "✨ Оптимизация промпта",
    "promptrollback": "↩️ Откат промпта",
}


def maint_keyboard() -> InlineKeyboardMarkup:
    rows = [[(label, f"menu:ask:{cmd}")] for cmd, label in MAINT_COMMANDS.items()]
    rows.append([("⬅️ Назад", "menu:ai")])
    return _kb(rows)


def confirm_keyboard(cmd: str) -> InlineKeyboardMarkup:
    return _kb([[("✅ Да", f"menu:run:{cmd}"), ("Отмена", "menu:maint")]])


HOUR_GRID: tuple[str, ...] = ("1", "1-2", "2", "3", "4", "6", "8", "24")


def _hour_order(selected: list[str]) -> list[str]:
    """Grid options, then any custom option already saved (e.g. «5» set by /taketimes)."""
    return list(HOUR_GRID) + [o for o in selected if o not in HOUR_GRID]


def hours_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        (("✅ " if o in selected else "") + o.replace("-", "–") + " ч", f"menu:hour:{o}")
        for o in _hour_order(selected)
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append(_BACK)
    return _kb(rows)


def toggle_hour(selected: list[str], option: str) -> list[str] | None:
    """Selection after tapping `option`, in grid order; None if it would leave 0 or more than 6."""
    chosen = set(selected) ^ {option}
    new = [o for o in _hour_order(selected) if o in chosen]
    return new if 1 <= len(new) <= 6 else None


HUB_TITLE = "🤖 <b>Меню бота</b>\nВыберите действие:"

HOWTO_TEXT = (
    "❓ <b>Как отвечать из топика тикета</b>\n\n"
    "1. Напиши в топик текст, фото, альбом или файл — как обычно.\n"
    "2. Бот спросит «Куда отправить?»: 📤 Клиенту, 💬 Комментарий или ✖️.\n"
    "3. После отправки кнопка 🗑 удалит сообщение из HDE (спросит подтверждение).\n\n"
    "• Исправил своё сообщение в Telegram — оно исправится и в HDE.\n"
    "• Голосовое — заметка по звонку в контекст тикета, клиенту не уходит.\n"
    "• 🪄 Автополя — под последним сообщением клиента, рядом с 💡.\n\n"
    "<i>Команды /send, /note, /delete, /autofill тоже работают.</i>"
)
```

Note: `toggle_hour` with an option missing from both the grid and `selected` adds it to `chosen` but `_hour_order` never lists it, so the result omits it — callers validate the option with `is_busy_option` and only grid/saved options are ever shown as buttons.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_command_menu.py -q -k "not callback"`
Expected: all selected tests PASS. (The two `test_menu_callback_*` tests may fail until Task 3 — `commands.py` still imports removed names.)

- [ ] **Step 5: Do not commit yet**

`bot/handlers/commands.py` still imports `submenu_keyboard` / `ARGS_HELP_TEXT`, so the bot would not start at this commit. `bot/command_menu.py` is committed together with Task 3.

---

### Task 2: Shared bodies for status, refresh, vacation, reports, optimizer

**Files:**
- Modify: `bot/handlers/commands.py` (`cmd_status`, `cmd_vacation`, `cmd_refresh`)
- Modify: `bot/handlers/report_commands.py` (`cmd_report`, `cmd_weekly`)
- Modify: `bot/handlers/optimizer_commands.py` (`cmd_aioptimize`, `cmd_promptrollback`)
- Test: `tests/test_command_menu.py` (append)

**Interfaces:**
- Produces:
  - `bot.handlers.commands.status_text() -> Awaitable[str]` — same text as `/status`, metrics block included
  - `bot.handlers.commands.run_refresh(target: Message) -> Awaitable[None]` — answers into `target`'s chat
  - `bot.handlers.commands.vacation_until_from_args(args: str, now_utc: datetime) -> datetime | None` — `""` → `next_work_start()`, `"3d"` → `now+3d`, `"YYYY-MM-DD"` → that day 09:00 MSK, else `None`
  - `bot.handlers.commands.turn_on_vacation(target: Message, until: datetime) -> Awaitable[None]`
  - `bot.handlers.commands.turn_off_vacation(target: Message) -> Awaitable[None]`
  - `bot.handlers.report_commands.run_daily_report(target: Message, report_date: date | None) -> Awaitable[None]`
  - `bot.handlers.report_commands.run_weekly(target: Message, today: date | None) -> Awaitable[None]`
  - `bot.handlers.optimizer_commands.run_aioptimize(message: Message) -> Awaitable[None]` (no auth check)
  - `bot.handlers.optimizer_commands.show_promptrollback(message: Message) -> Awaitable[None]` (no auth check)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_command_menu.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock


async def test_status_text_has_metrics(monkeypatch):
    import bot.handlers.commands as cmds
    for name in ("count_active_topics", "count_pending_delete_topics",
                 "count_pending_pre_sla_topics", "count_total_topics"):
        monkeypatch.setattr(cmds, name, AsyncMock(return_value=2))
    text = await cmds.status_text()
    assert "Активных topics" in text and "Метрики" in text


def test_vacation_until_from_args(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.work_schedule as ws
    now = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    marker = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ws, "next_work_start", lambda: marker)
    assert cmds.vacation_until_from_args("", now) == marker
    assert cmds.vacation_until_from_args("3d", now) == now + timedelta(days=3)
    assert cmds.vacation_until_from_args("2026-10-20", now) == datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    assert cmds.vacation_until_from_args("soon", now) is None


async def test_run_daily_report_passes_date(monkeypatch):
    import bot.handlers.report_commands as rc
    import bot.reporting.runner as runner
    from bot.config import config
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    run = AsyncMock(return_value="ok")
    monkeypatch.setattr(runner, "run_report", run)
    monkeypatch.setattr(rc, "mark_report_sent", AsyncMock())
    monkeypatch.setattr(config, "work_days", (0, 1, 2, 3, 4))
    target = MagicMock()
    target.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    await rc.run_daily_report(target, date(2026, 10, 12))
    run.assert_awaited_once_with(date(2026, 10, 12))


async def test_show_promptrollback_has_no_auth_gate(monkeypatch):
    import bot.handlers.optimizer_commands as oc
    import bot.db as db_module
    monkeypatch.setattr(db_module, "list_prompt_versions", AsyncMock(return_value=[]))
    msg = MagicMock()
    msg.from_user = MagicMock(id=999)            # not an admin, and it does not matter here
    msg.answer = AsyncMock()
    await oc.show_promptrollback(msg)
    msg.answer.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_command_menu.py -q -k "status_text or vacation_until or daily_report or promptrollback"`
Expected: FAIL — `AttributeError: module 'bot.handlers.commands' has no attribute 'status_text'` (and similar).

- [ ] **Step 3: Implement in `bot/handlers/commands.py`**

Replace `cmd_status` with:

```python
async def status_text() -> str:
    active_topics = await count_active_topics()
    pending_delete = await count_pending_delete_topics()
    pending_pre_sla = await count_pending_pre_sla_topics()
    total_topics = await count_total_topics()
    return (
        "📊 <b>Статус бота</b>\n\n"
        f"🟢 Активных topics: <b>{active_topics}</b>\n"
        f"⏳ Ожидают удаления: <b>{pending_delete}</b>\n"
        f"⏰ Ожидают pre-SLA: <b>{pending_pre_sla}</b>\n"
        f"📚 Всего записей: <b>{total_topics}</b>"
        + _format_metrics_block()
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await message.answer(await status_text(), parse_mode="HTML")
```

Replace `cmd_vacation` and `cmd_workon` with:

```python
def vacation_until_from_args(args: str, now_utc: datetime) -> datetime | None:
    """'' → next work start, '3d' → now + 3 days, 'YYYY-MM-DD' → 09:00 MSK that day; None if unparseable."""
    from .. import work_schedule
    import zoneinfo

    args = args.strip()
    if not args:
        return work_schedule.next_work_start()
    if args.endswith("d") and args[:-1].isdigit():
        return now_utc + timedelta(days=int(args[:-1]))
    try:
        d = date.fromisoformat(args)
    except ValueError:
        return None
    msk = zoneinfo.ZoneInfo("Europe/Moscow")
    return datetime(d.year, d.month, d.day, 9, 0, tzinfo=msk).astimezone(timezone.utc)


async def turn_on_vacation(target: Message, until: datetime) -> None:
    from ..work_schedule import enable_vacation
    import zoneinfo

    await enable_vacation(until)
    until_msk = until.astimezone(zoneinfo.ZoneInfo("Europe/Moscow"))
    await target.answer(
        f"🏖 <b>Режим отпуска включён</b>\n"
        f"Уведомления возобновятся: <b>{until_msk.strftime('%d.%m.%Y %H:%M')} МСК</b>",
        parse_mode="HTML",
    )


async def turn_off_vacation(target: Message) -> None:
    from ..work_schedule import disable_vacation, is_on_vacation

    if not is_on_vacation():
        await target.answer("ℹ️ Режим отпуска не активен.")
        return
    await disable_vacation()
    await target.answer("✅ <b>Режим отпуска отключён.</b> Уведомления возобновлены.", parse_mode="HTML")


@router.message(Command("vacation"))
async def cmd_vacation(message: Message, command: CommandObject) -> None:
    """
    /vacation        — до следующего рабочего дня
    /vacation 3d     — на N дней
    /vacation 2026-04-15  — до конкретной даты (МСК 09:00)
    """
    until = vacation_until_from_args(command.args or "", datetime.now(timezone.utc))
    if until is None:
        await message.answer(
            "⚠️ Неверный формат. Примеры:\n"
            "/vacation — до следующего рабочего дня\n"
            "/vacation 3d — на 3 дня\n"
            "/vacation 2026-04-15 — до 15 апреля",
            parse_mode="HTML",
        )
        return
    await turn_on_vacation(message, until)


@router.message(Command("workon"))
async def cmd_workon(message: Message) -> None:
    """Снять режим отпуска досрочно."""
    await turn_off_vacation(message)
```

Replace `cmd_refresh` with:

```python
async def run_refresh(target: Message) -> None:
    wait_msg = await target.answer("🔄 Синхронизирую с HDE...")
    try:
        result = await refresh_topics(bot=target.bot)
        text = format_refresh_result(
            active_count=result.active_after,
            hde_count=result.hde_count,
            created=result.created,
            renamed=result.renamed,
            deleted=result.deleted,
            cleaned_pending=result.cleaned_pending,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("refresh failed")
        text = f"⚠️ <b>Ошибка при синхронизации:</b> {exc}"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("refresh: wait message cleanup failed: %s", exc)
    await target.answer(text, parse_mode="HTML")

    # Retry AI summaries for topics that missed them
    from ..topic_manager import retry_missing_ai_summaries
    try:
        sent = await retry_missing_ai_summaries(target.bot)
        if sent:
            await target.answer(f"🧠 <b>AI саммари отправлено:</b> {sent} топик(ов)", parse_mode="HTML")
    except Exception as exc:
        logger.warning("retry_missing_ai_summaries failed: %s", exc)


@router.message(Command("refresh"))
async def cmd_refresh(message: Message) -> None:
    await run_refresh(message)
```

`date`, `datetime`, `timedelta`, `timezone` are already imported at the top of `commands.py`.

- [ ] **Step 4: Implement in `bot/handlers/report_commands.py`**

Replace the bodies so both commands parse arguments and delegate:

```python
_NOT_CONFIGURED = (
    "⚠️ Отчёт не настроен.\n\n"
    "Добавьте в .env:\n"
    "<code>HDE_REPORT_PASSWORD=...\n"
    "GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google_service_account.json\n"
    "GOOGLE_SPREADSHEET_ID=...</code>"
)


async def run_daily_report(target: Message, report_date: date | None) -> None:
    """Daily report for `report_date`, or for the last work day when None."""
    from ..reporting import runner
    from ..config import config
    from ..work_schedule import last_work_day

    if not runner.is_report_configured():
        await target.answer(_NOT_CONFIGURED, parse_mode="HTML")
        return
    day = report_date or last_work_day()
    if day.weekday() not in config.work_days:
        await target.answer(
            f"🏖 <b>{day.strftime('%d.%m.%Y')} был выходной</b> — данных для отчёта нет.",
            parse_mode="HTML",
        )
        return

    wait_msg = await target.answer("⏳ Отчёт генерируется, подождите...")
    try:
        result = await runner.run_report(report_date)
        from ..scheduler import mark_report_done_today
        mark_report_done_today()
        await mark_report_sent(day)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("report: wait message cleanup failed: %s", exc)
    await target.answer(result, parse_mode="HTML")


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject) -> None:
    """
    /report          — отчёт за прошлый рабочий день
    /report 2025-04-04  — отчёт за конкретную дату (YYYY-MM-DD)
    """
    report_date: date | None = None
    if command.args:
        try:
            report_date = date.fromisoformat(command.args.strip())
        except ValueError:
            await message.answer(
                "⚠️ Неверный формат даты. Используйте: <code>/report YYYY-MM-DD</code>",
                parse_mode="HTML",
            )
            return
    await run_daily_report(message, report_date)


async def run_weekly(target: Message, today: date | None) -> None:
    """Weekly summary as of Sunday `today` (None = real today)."""
    from ..reporting import runner

    if not runner.is_report_configured():
        await target.answer(_NOT_CONFIGURED, parse_mode="HTML")
        return
    wait_msg = await target.answer("⏳ Считаю недельную сводку...")
    try:
        result = await runner.run_weekly_summary(today)
    except Exception as exc:
        result = f"❌ <b>Ошибка:</b>\n<code>{exc}</code>"
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.debug("weekly: wait message cleanup failed: %s", exc)
    await target.answer(result, parse_mode="HTML")


@router.message(Command("weekly"))
async def cmd_weekly(message: Message, command: CommandObject) -> None:
    """
    /weekly            — сводка за прошлую рабочую неделю
    /weekly 2026-09-06 — сводка на конкретное воскресенье (YYYY-MM-DD)

    Нужна, когда воскресная авторассылка упала: планировщик помечает день
    отправленным до попытки и сам не повторяет.
    """
    today: date | None = None
    if command.args:
        try:
            today = date.fromisoformat(command.args.strip())
        except ValueError:
            await message.answer(
                "⚠️ Неверный формат даты. Используйте: <code>/weekly YYYY-MM-DD</code>",
                parse_mode="HTML",
            )
            return
    await run_weekly(message, today)
```

Calling `runner.run_report` / `runner.run_weekly_summary` through the module (not `from … import`) is what lets the tests monkeypatch them; check `tests/test_sheets_retry.py` and `tests/test_reporting.py` still pass — if they monkeypatch `bot.reporting.runner.<name>`, module access keeps that working.

- [ ] **Step 5: Implement in `bot/handlers/optimizer_commands.py`**

```python
async def run_aioptimize(message: Message) -> None:
    """Start the prompt optimizer in the background."""
    import asyncio
    from ..optimizer.agent import run_optimizer

    await message.answer("🧪 Запускаю оптимизатор промптов...")
    asyncio.create_task(run_optimizer(message.bot))


@router.message(Command("aioptimize"))
async def cmd_aioptimize(message: Message) -> None:
    """Manually trigger prompt optimizer (for testing)."""
    from ..config import config as _cfg
    if message.from_user and message.from_user.id not in _cfg.operator_telegram_user_ids:
        return
    await run_aioptimize(message)
```

and split `cmd_promptrollback` the same way: move everything after the auth check (from `versions = await list_prompt_versions(limit=8)` to the final `message.answer`) into `async def show_promptrollback(message: Message) -> None`, with `from html import escape` and `from .. import db as _db` at its top, and call `await _db.list_prompt_versions(limit=8)` (module access so the test can monkeypatch it). `cmd_promptrollback` keeps only the auth check and `await show_promptrollback(message)`.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_command_menu.py tests/test_report_vacation.py tests/test_sheets_retry.py tests/test_reporting.py tests/test_quiet_mode.py -q`
Expected: the new tests PASS; existing report/vacation tests PASS. (`test_menu_callback_*` may still fail until Task 3.)

- [ ] **Step 7: Commit**

```bash
git add bot/handlers/commands.py bot/handlers/report_commands.py bot/handlers/optimizer_commands.py tests/test_command_menu.py
git commit -m "refactor(commands): share status, refresh, vacation, report and optimizer bodies with the menu"
```

---

### Task 3: Menu handlers — /start, ☰, screens and actions

**Files:**
- Modify: `bot/handlers/commands.py` (`cmd_menu`, `cb_menu`, import line 28)
- Test: `tests/test_command_menu.py` (replace the two old async tests, append new)

**Interfaces:**
- Consumes: everything Task 1 and Task 2 produce; `bot.general_channel.TAKE_HOURS_SETTING`, `busy_options()`, `is_busy_option()`; `bot.work_schedule.is_on_vacation()`, `vacation_until()`, `_today_msk()`; `bot.db.get_setting/set_setting`.
- Produces: `cmd_menu(message)`, `cb_menu(callback)` handlers; `menu_screen(name: str) -> Awaitable[tuple[str, InlineKeyboardMarkup] | None]`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_command_menu.py` delete the old `test_menu_callback_status` and `test_menu_callback_navigation`, then append:

```python
def _callback(data, user_id=123456789):
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=user_id)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    return cb


async def test_menu_status_button_matches_command(monkeypatch):
    import bot.handlers.commands as cmds
    monkeypatch.setattr(cmds, "status_text", AsyncMock(return_value="STATUS"))
    cb = _callback("menu:status")
    await cmds.cb_menu(cb)
    cb.message.answer.assert_awaited_once_with("STATUS", parse_mode="HTML")


@pytest.mark.parametrize("screen", ["root", "quiet", "reports", "days", "weeks", "ai", "maint", "hours", "howto"])
async def test_every_screen_renders(initialized_db, screen):
    import bot.handlers.commands as cmds
    cb = _callback(f"menu:{screen}")
    await cmds.cb_menu(cb)
    cb.message.edit_text.assert_awaited_once()


async def test_vacation_buttons(monkeypatch, initialized_db):
    import bot.handlers.commands as cmds
    seen = []
    monkeypatch.setattr(cmds, "turn_on_vacation", AsyncMock(side_effect=lambda t, until: seen.append(until)))
    cb = _callback("menu:vac:3")
    before = datetime.now(timezone.utc)
    await cmds.cb_menu(cb)
    assert timedelta(days=3) <= seen[0] - before < timedelta(days=3, minutes=1)


async def test_report_day_button(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.handlers.report_commands as rc
    run = AsyncMock()
    monkeypatch.setattr(rc, "run_daily_report", run)
    cb = _callback("menu:rep:2026-10-06")
    await cmds.cb_menu(cb)
    run.assert_awaited_once_with(cb.message, date(2026, 10, 6))


async def test_week_button(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.handlers.report_commands as rc
    run = AsyncMock()
    monkeypatch.setattr(rc, "run_weekly", run)
    cb = _callback("menu:week:2026-10-11")
    await cmds.cb_menu(cb)
    run.assert_awaited_once_with(cb.message, date(2026, 10, 11))


async def test_hour_toggle_saves_and_rejects(initialized_db):
    import bot.handlers.commands as cmds
    from bot.db import get_setting, set_setting
    from bot.general_channel import TAKE_HOURS_SETTING
    await set_setting(TAKE_HOURS_SETTING, "1 4")
    await cmds.cb_menu(_callback("menu:hour:2"))
    assert await get_setting(TAKE_HOURS_SETTING) == "1 2 4"
    await set_setting(TAKE_HOURS_SETTING, "1")
    cb = _callback("menu:hour:1")
    await cmds.cb_menu(cb)
    assert await get_setting(TAKE_HOURS_SETTING) == "1"
    cb.answer.assert_awaited_with("От 1 до 6 вариантов", show_alert=True)


async def test_hour_toggle_ignores_garbage(initialized_db):
    import bot.handlers.commands as cmds
    from bot.db import get_setting, set_setting
    from bot.general_channel import TAKE_HOURS_SETTING
    await set_setting(TAKE_HOURS_SETTING, "1 4")
    await cmds.cb_menu(_callback("menu:hour:99"))
    assert await get_setting(TAKE_HOURS_SETTING) == "1 4"


async def test_maint_run_only_whitelisted(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.handlers.ai_commands as ai
    reindex = AsyncMock()
    monkeypatch.setattr(ai, "cmd_aireindex", reindex)
    await cmds.cb_menu(_callback("menu:run:rm-rf"))
    reindex.assert_not_awaited()
    await cmds.cb_menu(_callback("menu:run:aireindex"))
    reindex.assert_awaited_once()


async def test_maint_optimizer_needs_admin(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.handlers.optimizer_commands as oc
    run = AsyncMock()
    monkeypatch.setattr(oc, "run_aioptimize", run)
    await cmds.cb_menu(_callback("menu:run:aioptimize", user_id=555))
    run.assert_not_awaited()
    await cmds.cb_menu(_callback("menu:run:aioptimize", user_id=123456789))
    run.assert_awaited_once()


async def test_start_in_private_sets_menu_keyboard_once(initialized_db):
    import bot.handlers.commands as cmds
    msg = MagicMock()
    msg.chat = MagicMock(id=42, type="private")
    msg.answer = AsyncMock()
    await cmds.cmd_menu(msg)
    await cmds.cmd_menu(msg)
    reply_kb_sends = [c for c in msg.answer.await_args_list
                      if type(c.kwargs.get("reply_markup")).__name__ == "ReplyKeyboardMarkup"]
    assert len(reply_kb_sends) == 1
    assert msg.answer.await_count == 3        # keyboard note once + hub twice


async def test_menu_in_group_has_no_reply_keyboard(initialized_db):
    import bot.handlers.commands as cmds
    msg = MagicMock()
    msg.chat = MagicMock(id=-100, type="supergroup")
    msg.answer = AsyncMock()
    await cmds.cmd_menu(msg)
    assert msg.answer.await_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_command_menu.py -q`
Expected: FAIL — `ImportError` on `submenu_keyboard`/`ARGS_HELP_TEXT` in `commands.py`, or missing behaviour.

- [ ] **Step 3: Implement**

In `bot/handlers/commands.py` replace the import on line 28 with:

```python
from .. import command_menu as menu
```

Replace `cmd_menu` with:

```python
@router.message(Command("start"))
@router.message(Command("help"))
@router.message(Command("menu"))
@router.message(F.text == menu.MENU_BUTTON_TEXT)
async def cmd_menu(message: Message) -> None:
    if message.chat.type == "private":
        # A reply keyboard persists once sent, so set the ☰ button once per chat.
        key = f"menu_keyboard_sent:{message.chat.id}"
        if not await db.get_setting(key):
            await message.answer("☰ Кнопка меню теперь всегда под полем ввода.",
                                 reply_markup=menu.menu_reply_keyboard())
            await db.set_setting(key, "1")
    await message.answer(menu.HUB_TITLE, parse_mode="HTML", reply_markup=menu.hub_keyboard())
```

Replace the whole `cb_menu` with:

```python
async def menu_screen(name: str):
    """(text, keyboard) for a menu screen, or None if `name` is not a screen."""
    from ..config import config
    from ..general_channel import busy_options
    from ..work_schedule import _today_msk, is_on_vacation, vacation_until
    import zoneinfo

    if name == "root":
        return menu.HUB_TITLE, menu.hub_keyboard()
    if name == "quiet":
        active = is_on_vacation()
        if active:
            until = vacation_until().astimezone(zoneinfo.ZoneInfo("Europe/Moscow"))
            title = f"💤 <b>Тишина до {until:%d.%m %H:%M} МСК</b>"
        else:
            title = "💤 <b>Тишина выключена</b>"
        return title + "\nВключить на:", menu.quiet_keyboard(active)
    if name == "reports":
        return "📈 <b>Отчёты</b>", menu.reports_keyboard()
    if name == "days":
        return "📅 <b>Отчёт за день</b>", menu.days_keyboard(_today_msk(), config.work_days)
    if name == "weeks":
        return "🗓 <b>Недельная сводка</b>", menu.weeks_keyboard(_today_msk())
    if name == "ai":
        on = await db.get_setting("ai_summary_enabled", "1") == "1"
        return "🤖 <b>AI</b>", menu.ai_keyboard(on)
    if name == "maint":
        return ("🛠 <b>Обслуживание AI</b>\nТяжёлые операции, каждая спросит подтверждение.",
                menu.maint_keyboard())
    if name == "hours":
        return ("⏳ <b>Часы на кнопках «Беру»</b>\nОтметь от 1 до 6 вариантов. "
                "Новые тикеты получат их сразу, уже отправленные — нет.",
                menu.hours_keyboard(await busy_options()))
    if name == "howto":
        return menu.HOWTO_TEXT, menu.back_keyboard()
    return None


async def _show(callback: CallbackQuery, name: str) -> None:
    text, kb = await menu_screen(name)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:  # "message is not modified" on a repeated tap
        logger.debug("cb_menu: screen %s not redrawn: %s", name, exc)


async def _run_maint(callback: CallbackQuery, cmd: str) -> None:
    from aiogram.filters import CommandObject
    from ..config import config
    from . import ai_commands, optimizer_commands

    target = callback.message
    if cmd in ("aioptimize", "promptrollback") and callback.from_user.id not in config.operator_telegram_user_ids:
        await target.answer("⛔ Только для администратора бота")
        return
    if cmd == "aiimport":
        await ai_commands.cmd_aiimport(target, CommandObject(command="aiimport"))
    elif cmd == "aireindex":
        await ai_commands.cmd_aireindex(target)
    elif cmd == "aibackfill":
        await ai_commands.cmd_aibackfill(target)
    elif cmd == "aianalyze":
        await ai_commands.cmd_aianalyze(target)
    elif cmd == "aioptimize":
        await optimizer_commands.run_aioptimize(target)
    elif cmd == "promptrollback":
        await optimizer_commands.show_promptrollback(target)


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery) -> None:
    _, action, *rest = callback.data.split(":")
    arg = rest[0] if rest else ""

    if action == "hour":
        await _toggle_hour(callback, arg)
        return
    await callback.answer()

    if await menu_screen(action) is not None:
        await _show(callback, action)
        return

    target = callback.message
    if action == "status":
        await target.answer(await status_text(), parse_mode="HTML")
    elif action == "refresh":
        await run_refresh(target)
    elif action == "vac":
        until = vacation_until_from_args("" if arg == "next" else f"{arg}d", datetime.now(timezone.utc))
        if until is not None:
            await turn_on_vacation(target, until)
            await _show(callback, "quiet")
    elif action == "workon":
        await turn_off_vacation(target)
        await _show(callback, "quiet")
    elif action in ("report_yesterday", "rep", "week"):
        from . import report_commands
        try:
            day = date.fromisoformat(arg) if arg else None
        except ValueError:
            logger.warning("cb_menu: bad date %r", arg)
            return
        if action == "week":
            await report_commands.run_weekly(target, day)
        else:
            await report_commands.run_daily_report(target, day)
    elif action == "digest":
        try:
            await send_morning_digest(callback.bot)
            await target.answer("🌅 Утренняя сводка отправлена.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("cb_menu digest failed")
            await target.answer(f"⚠️ <b>Ошибка:</b> {exc}", parse_mode="HTML")
    elif action == "aisummary":
        new = "0" if await db.get_setting("ai_summary_enabled", "1") == "1" else "1"
        await db.set_setting("ai_summary_enabled", new)
        await _show(callback, "ai")
    elif action == "aimetrics":
        from .ai_commands import cmd_aimetrics
        await cmd_aimetrics(target)
    elif action == "aiknowledge":
        from .ai_commands import cmd_aiknowledge
        await cmd_aiknowledge(target)
    elif action == "ask" and arg in menu.MAINT_COMMANDS:
        await target.edit_text(f"Запустить «{menu.MAINT_COMMANDS[arg]}»?",
                               reply_markup=menu.confirm_keyboard(arg))
    elif action == "run" and arg in menu.MAINT_COMMANDS:
        await _show(callback, "maint")
        await _run_maint(callback, arg)
    else:
        logger.warning("cb_menu: unknown action %r", callback.data)


async def _toggle_hour(callback: CallbackQuery, option: str) -> None:
    from ..general_channel import TAKE_HOURS_SETTING, busy_options, is_busy_option

    if not is_busy_option(option):
        await callback.answer()
        return
    new = menu.toggle_hour(await busy_options(), option)
    if new is None:
        await callback.answer("От 1 до 6 вариантов", show_alert=True)
        return
    await db.set_setting(TAKE_HOURS_SETTING, " ".join(new))
    await callback.answer()
    await _show(callback, "hours")
```

Note on `run_weekly` / `run_daily_report` being looked up as `report_commands.<name>` at call time — that is what lets `test_report_day_button` / `test_week_button` monkeypatch them.

Note on `_run_maint` calling `ai_commands.cmd_aireindex` etc. through the module — same reason (`test_maint_run_only_whitelisted`).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_command_menu.py -q`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q -x`
Expected: all PASS (was 1088). Any failure that references `ARGS_HELP_TEXT`, `submenu_keyboard`, `back_keyboard` import or `menu:args` is a leftover caller — fix the caller, not the test.

- [ ] **Step 6: Commit**

```bash
git add bot/command_menu.py bot/handlers/commands.py tests/test_command_menu.py
git commit -m "feat(menu): every action and argument variant behind buttons, /start and ☰ open the hub"
```

---

### Task 4: Deploy stage 1 and check it live

**Files:** none changed.

- [ ] **Step 1: Push**

```bash
git push origin main
```

- [ ] **Step 2: Deploy on config1**

```bash
ssh config1 "cd /opt/hde-bot && sudo -n git pull --ff-only && sudo -n systemctl restart hde-bot && sleep 5 && systemctl is-active hde-bot"
```
Expected: `active`.

- [ ] **Step 3: Check the log**

```bash
ssh config1 "sudo -n tail -n 80 /opt/hde-bot/logs/bot.log | grep -E 'ERROR|Traceback|set_my_commands|Start polling' | tail -20"
```
Expected: no `ERROR`/`Traceback` after the restart time.

- [ ] **Step 4: Owner checks in Telegram** (ask via os-step-by-step): `/start` in the bot DM → note «☰ Кнопка меню…» + hub; tap through every screen; «Часы» toggle one option and toggle it back; do **not** press «Тишина».

---

### Task 5: Topic question «Куда отправить?» with send and delete

**Files:**
- Create: `bot/handlers/topic_actions.py`
- Modify: `bot/handlers/commands.py` (sub-router includes at the bottom)
- Test: `tests/test_topic_actions.py`

**Interfaces:**
- Consumes: `bot.operator_replies.get_operator_topic_context`, `send_public_reply`, `add_internal_note`, `delete_operator_message`, `cache_incoming_topic_media`, `format_operator_exception`, `OperatorReplyError`; `bot.transcription.extract_audio_meta`; `config.is_operator_allowed`.
- Produces: `router`; `wants_question(message) -> bool`; `ask_keyboard()`, `delete_keyboard(orig_id: int)`, `confirm_delete_keyboard(orig_id: int)`; handlers `ask_where_to_send`, `cb_send`, `cb_cancel`, `cb_delete`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_topic_actions.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.handlers.topic_actions as ta
from bot.operator_replies import OperatorReplyError

ENGINEER = 123456789          # in config.operator_telegram_user_ids (conftest)
CHAT = -100123456789


def make_msg(*, text=None, user_id=ENGINEER, is_bot=False, thread=9001, media_group_id=None,
             photo=None, document=None, audio=None, voice=None, video=None, animation=None, message_id=10):
    msg = MagicMock()
    msg.message_id = message_id
    msg.message_thread_id = thread
    msg.chat = SimpleNamespace(id=CHAT)
    msg.from_user = SimpleNamespace(id=user_id, is_bot=is_bot)
    msg.text, msg.caption = text, None
    msg.photo, msg.document, msg.audio, msg.voice = photo, document, audio, voice
    msg.video, msg.animation, msg.video_note = video, animation, None
    msg.media_group_id = media_group_id
    msg.reply = AsyncMock()
    return msg


@pytest.fixture(autouse=True)
def _clean_state():
    ta._seen_albums.clear()
    ta._in_flight.clear()


@pytest.mark.parametrize("kwargs,expected", [
    (dict(text="Перезагрузите кассу"), True),
    (dict(photo=[object()]), True),
    (dict(document=SimpleNamespace(mime_type="application/pdf", file_name="act.pdf")), True),
    (dict(text="/send hi"), False),
    (dict(text="hi", user_id=555), False),                     # not an engineer
    (dict(text="hi", is_bot=True), False),
    (dict(voice=SimpleNamespace(file_id="v")), False),         # voice → call note
    (dict(audio=SimpleNamespace(file_id="a", file_name="call.mp3", mime_type="audio/mpeg")), False),
    (dict(document=SimpleNamespace(file_id="d", mime_type="audio/wav", file_name="call.wav")), False),
])
def test_wants_question(kwargs, expected):
    assert ta.wants_question(make_msg(**kwargs)) is expected


def test_topic_router_comes_before_media_router():
    import bot.handlers.commands as cmds
    from bot.handlers.media_commands import router as media_router
    subs = cmds.router.sub_routers
    assert subs.index(ta.router) < subs.index(media_router)


async def test_question_is_a_reply(monkeypatch):
    monkeypatch.setattr(ta, "cache_incoming_topic_media", AsyncMock())
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock())
    msg = make_msg(text="Перезагрузите кассу")
    await ta.ask_where_to_send(msg)
    msg.reply.assert_awaited_once()
    assert msg.reply.await_args.args[0] == "Куда отправить?"


async def test_no_question_outside_active_ticket(monkeypatch):
    cache = AsyncMock()
    monkeypatch.setattr(ta, "cache_incoming_topic_media", cache)
    monkeypatch.setattr(ta, "get_operator_topic_context",
                        AsyncMock(side_effect=OperatorReplyError("Topic не активен для ответа")))
    msg = make_msg(text="hi")
    await ta.ask_where_to_send(msg)
    msg.reply.assert_not_awaited()
    cache.assert_awaited_once()          # media cache still filled


async def test_album_gets_one_question(monkeypatch):
    cache = AsyncMock()
    monkeypatch.setattr(ta, "cache_incoming_topic_media", cache)
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock())
    msgs = [make_msg(photo=[object()], media_group_id="alb1", message_id=i) for i in (1, 2, 3)]
    for m in msgs:
        await ta.ask_where_to_send(m)
    assert sum(m.reply.await_count for m in msgs) == 1
    assert cache.await_count == 3


def make_callback(data, *, original=None, user_id=ENGINEER, prompt_id=50):
    cb = MagicMock()
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id)
    cb.answer = AsyncMock()
    cb.bot = MagicMock()
    prompt = MagicMock()
    prompt.message_id = prompt_id
    prompt.message_thread_id = 9001
    prompt.chat = SimpleNamespace(id=CHAT)
    prompt.reply_to_message = original
    prompt.edit_text = AsyncMock()
    prompt.edit_reply_markup = AsyncMock()
    prompt.delete = AsyncMock()
    cb.message = prompt
    return cb


async def test_client_button_sends_with_original_id(monkeypatch):
    send = AsyncMock(return_value="✅ sent")
    monkeypatch.setattr(ta, "send_public_reply", send)
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock(return_value="CTX"))
    original = make_msg(text="Перезагрузите кассу", message_id=77)
    cb = make_callback("tp:client", original=original)
    await ta.cb_send(cb)
    kw = send.await_args.kwargs
    assert kw["tg_message_id"] == 77 and kw["message"] is cb.message and kw["command_args"] is None
    cb.message.edit_reply_markup.assert_awaited()            # keyboard gone before sending
    text, = cb.message.edit_text.await_args.args
    assert text == "✅ sent"
    assert cb.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "tp:del:77"


async def test_note_button_uses_internal_note(monkeypatch):
    note = AsyncMock(return_value="📝 saved")
    monkeypatch.setattr(ta, "add_internal_note", note)
    monkeypatch.setattr(ta, "send_public_reply", AsyncMock())
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock(return_value="CTX"))
    await ta.cb_send(make_callback("tp:note", original=make_msg(text="x", message_id=5)))
    note.assert_awaited_once()
    ta.send_public_reply.assert_not_awaited()


async def test_double_tap_sends_once(monkeypatch):
    import asyncio
    gate = asyncio.Event()

    async def slow_send(**kwargs):
        await gate.wait()
        return "ok"

    send = AsyncMock(side_effect=slow_send)
    monkeypatch.setattr(ta, "send_public_reply", send)
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock(return_value="CTX"))
    original = make_msg(text="x", message_id=7)
    first = asyncio.create_task(ta.cb_send(make_callback("tp:client", original=original)))
    await asyncio.sleep(0)
    await ta.cb_send(make_callback("tp:client", original=original))
    gate.set()
    await first
    assert send.await_count == 1


async def test_non_engineer_press_sends_nothing(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(ta, "send_public_reply", send)
    monkeypatch.setattr(ta, "get_operator_topic_context",
                        AsyncMock(side_effect=OperatorReplyError("У вас нет прав для этой команды")))
    cb = make_callback("tp:client", original=make_msg(text="x"), user_id=555)
    await ta.cb_send(cb)
    send.assert_not_awaited()
    kwargs = cb.message.edit_text.await_args.kwargs
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "tp:client"   # buttons back
    assert "нет прав" in cb.message.edit_text.await_args.args[0]


async def test_original_deleted(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(ta, "send_public_reply", send)
    cb = make_callback("tp:client", original=None)
    await ta.cb_send(cb)
    send.assert_not_awaited()
    cb.message.delete.assert_awaited_once()


async def test_cancel_deletes_question():
    cb = make_callback("tp:cancel", original=make_msg(text="x"))
    await ta.cb_cancel(cb)
    cb.message.delete.assert_awaited_once()


async def test_delete_needs_confirmation(monkeypatch):
    delete = AsyncMock(return_value="🗑 deleted")
    monkeypatch.setattr(ta, "delete_operator_message", delete)
    monkeypatch.setattr(ta, "get_operator_topic_context", AsyncMock(return_value="CTX"))
    cb = make_callback("tp:del:77")
    await ta.cb_delete(cb)
    delete.assert_not_awaited()
    confirm = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in confirm.inline_keyboard for b in row] == ["tp:delyes:77", "tp:delno:77"]

    cb = make_callback("tp:delyes:77")
    await ta.cb_delete(cb)
    delete.assert_awaited_once_with(context="CTX", telegram_message_id=77)
    assert cb.message.edit_text.await_args.args[0] == "🗑 deleted"


async def test_delete_no_restores_button():
    cb = make_callback("tp:delno:77")
    await ta.cb_delete(cb)
    kb = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "tp:del:77"


async def test_delete_rejects_garbage_id(monkeypatch):
    delete = AsyncMock()
    monkeypatch.setattr(ta, "delete_operator_message", delete)
    await ta.cb_delete(make_callback("tp:delyes:abc"))
    delete.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_topic_actions.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.handlers.topic_actions'`.

- [ ] **Step 3: Create `bot/handlers/topic_actions.py`**

```python
"""Ticket-topic actions: write as usual, then pick where it goes with a button.

The bot's question is a reply to the engineer's message, so a button press
brings the original back in callback.message.reply_to_message and nothing has
to be stored. Sending goes through the same functions as /send and /note,
keyed by the original message id, so editing that message later edits the
HDE post too (on_edited_message).
"""
import logging
from collections import deque

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import config
from ..operator_replies import (
    OperatorReplyError,
    add_internal_note,
    cache_incoming_topic_media,
    delete_operator_message,
    format_operator_exception,
    get_operator_topic_context,
    send_public_reply,
)
from ..transcription import extract_audio_meta

logger = logging.getLogger(__name__)
router = Router()

ASK_TEXT = "Куда отправить?"

# An album arrives as several messages sharing media_group_id: ask once per album.
_seen_albums: deque[str] = deque(maxlen=200)
# Questions whose send/delete is running: a second tap must not post twice.
_in_flight: set[tuple[int, int]] = set()


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def ask_keyboard() -> InlineKeyboardMarkup:
    return _kb([[("📤 Клиенту", "tp:client"), ("💬 Комментарий", "tp:note"), ("✖️", "tp:cancel")]])


def delete_keyboard(orig_id: int) -> InlineKeyboardMarkup:
    return _kb([[("🗑 Удалить из HDE", f"tp:del:{orig_id}")]])


def confirm_delete_keyboard(orig_id: int) -> InlineKeyboardMarkup:
    return _kb([[("Да, удалить", f"tp:delyes:{orig_id}"), ("Нет", f"tp:delno:{orig_id}")]])


def wants_question(message: Message) -> bool:
    """An engineer's own text/photo/video/file/GIF in a topic; not a command, voice or call recording.

    Anything rejected here falls through to the media router as before
    (voice → call note, audio → transcription, everything → media cache).
    """
    user = message.from_user
    if user is None or user.is_bot or not config.is_operator_allowed(user.id):
        return False
    if (message.text or "").startswith("/"):
        return False
    if not (message.text or message.photo or message.video or message.document or message.animation):
        return False
    return extract_audio_meta(message) is None


@router.message(F.message_thread_id.is_not(None), wants_question)
async def ask_where_to_send(message: Message) -> None:
    await cache_incoming_topic_media(message)   # /send-by-reply and albums read this cache
    if message.media_group_id:
        if message.media_group_id in _seen_albums:
            return
        _seen_albums.append(message.media_group_id)
    try:
        await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
            chat_id=message.chat.id,
        )
    except OperatorReplyError:
        return   # General, a closed ticket or an unknown topic: nowhere to send
    await message.reply(ASK_TEXT, reply_markup=ask_keyboard(), disable_notification=True)


@router.callback_query(F.data.in_({"tp:client", "tp:note"}))
async def cb_send(callback: CallbackQuery) -> None:
    prompt = callback.message
    original = prompt.reply_to_message
    if original is None:
        await callback.answer("Исходное сообщение удалено, отправлять нечего", show_alert=True)
        try:
            await prompt.delete()
        except Exception as exc:
            logger.debug("topic_actions: question cleanup failed: %s", exc)
        return
    key = (prompt.chat.id, prompt.message_id)
    if key in _in_flight:
        await callback.answer()
        return
    _in_flight.add(key)
    try:
        await _send(callback, prompt, original)
    finally:
        _in_flight.discard(key)


async def _send(callback: CallbackQuery, prompt: Message, original: Message) -> None:
    to_client = callback.data == "tp:client"
    try:
        await prompt.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("topic_actions: keyboard removal failed: %s", exc)
    try:
        context = await get_operator_topic_context(
            telegram_user_id=callback.from_user.id,
            topic_id=prompt.message_thread_id,
            chat_id=prompt.chat.id,
        )
        send = send_public_reply if to_client else add_internal_note
        result = await send(
            bot=callback.bot, context=context, message=prompt,
            command_args=None, tg_message_id=original.message_id,
        )
    except Exception as exc:
        if not isinstance(exc, OperatorReplyError):
            logger.exception("topic_actions: send failed")
        await callback.answer()
        await prompt.edit_text(f"{ASK_TEXT}\n{format_operator_exception(exc)}",
                               parse_mode="HTML", reply_markup=ask_keyboard())
        return
    await callback.answer("Отправлено клиенту" if to_client else "Комментарий сохранён")
    await prompt.edit_text(result, parse_mode="HTML", reply_markup=delete_keyboard(original.message_id))


@router.callback_query(F.data == "tp:cancel")
async def cb_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception as exc:
        logger.debug("topic_actions: question cleanup failed: %s", exc)


@router.callback_query(F.data.startswith("tp:del"))
async def cb_delete(callback: CallbackQuery) -> None:
    _, action, raw_id = (callback.data.split(":") + ["", ""])[:3]
    if not raw_id.isdigit():
        await callback.answer()
        return
    orig_id = int(raw_id)
    prompt = callback.message
    if action == "del":
        await callback.answer()
        await prompt.edit_reply_markup(reply_markup=confirm_delete_keyboard(orig_id))
        return
    if action == "delno":
        await callback.answer()
        await prompt.edit_reply_markup(reply_markup=delete_keyboard(orig_id))
        return
    if action != "delyes":
        await callback.answer()
        return
    key = (prompt.chat.id, prompt.message_id)
    if key in _in_flight:
        await callback.answer()
        return
    _in_flight.add(key)
    try:
        await prompt.edit_reply_markup(reply_markup=None)
        context = await get_operator_topic_context(
            telegram_user_id=callback.from_user.id,
            topic_id=prompt.message_thread_id,
            chat_id=prompt.chat.id,
        )
        result = await delete_operator_message(context=context, telegram_message_id=orig_id)
    except Exception as exc:
        if not isinstance(exc, OperatorReplyError):
            logger.exception("topic_actions: delete failed")
        await callback.answer(str(exc)[:190] or "Не удалось удалить", show_alert=True)
        await prompt.edit_reply_markup(reply_markup=delete_keyboard(orig_id))
        return
    finally:
        _in_flight.discard(key)
    await callback.answer("Удалено из HDE")
    await prompt.edit_text(result, parse_mode="HTML")
```

- [ ] **Step 4: Include the router**

At the bottom of `bot/handlers/commands.py` change the include block to:

```python
from .ai_commands import router as _ai_router  # noqa: E402
from .optimizer_commands import router as _optimizer_router  # noqa: E402
from .report_commands import router as _report_router  # noqa: E402
from .topic_actions import router as _topic_router  # noqa: E402
from .media_commands import router as _media_router  # noqa: E402

router.include_router(_ai_router)
router.include_router(_optimizer_router)
router.include_router(_report_router)
# Before media: it must see an engineer's documents before the call-recording
# handler (F.document) and the catch-all media cache swallow them.
router.include_router(_topic_router)
router.include_router(_media_router)
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_topic_actions.py tests/test_operator_replies.py tests/test_call_notes.py -q`
Expected: all PASS.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q -x`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add bot/handlers/topic_actions.py bot/handlers/commands.py tests/test_topic_actions.py
git commit -m "feat(topics): write in a ticket topic, then send it to the client or as a comment with a button"
```

---

### Task 6: 🪄 Автополя button

**Files:**
- Modify: `bot/handlers/commands.py` (`cmd_autofill`)
- Modify: `bot/handlers/ai_feedback.py:42-46` (`suggest_button_kb`)
- Modify: `bot/handlers/topic_actions.py` (append `cb_autofill`)
- Test: `tests/test_topic_actions.py` (append)

**Interfaces:**
- Produces: `bot.handlers.commands.run_autofill(bot, *, chat_id: int, topic_id: int, tg_user_id: int) -> Awaitable[str]` (HTML result text; raises `OperatorReplyError` outside an active ticket topic); `tp:autofill` callback.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_topic_actions.py`:

```python
def test_suggest_keyboard_has_autofill():
    from bot.handlers.ai_feedback import suggest_button_kb
    data = [b.callback_data for row in suggest_button_kb().inline_keyboard for b in row]
    assert data == ["ai:suggest", "tp:autofill"]


async def test_autofill_button_answers_in_topic(monkeypatch):
    import bot.handlers.commands as cmds
    run = AsyncMock(return_value="✅ fields")
    monkeypatch.setattr(cmds, "run_autofill", run)
    cb = make_callback("tp:autofill")
    cb.message.answer = AsyncMock()
    await ta.cb_autofill(cb)
    run.assert_awaited_once_with(cb.bot, chat_id=CHAT, topic_id=9001, tg_user_id=ENGINEER)
    cb.message.answer.assert_awaited_once_with("✅ fields", parse_mode="HTML")


async def test_autofill_button_ai_off(monkeypatch, initialized_db):
    import bot.handlers.commands as cmds
    import bot.operators as ops
    monkeypatch.setattr(cmds, "get_operator_topic_context", AsyncMock(return_value=SimpleNamespace(
        record=SimpleNamespace(ticket_id="T1", ticket_name=""), topic_id=9001)))
    monkeypatch.setattr(ops, "ai_enabled_for", lambda **kw: False)
    text = await cmds.run_autofill(MagicMock(), chat_id=CHAT, topic_id=9001, tg_user_id=ENGINEER)
    assert ops.AI_OFF_NOTE in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_topic_actions.py -q -k autofill`
Expected: FAIL — `AttributeError: ... has no attribute 'cb_autofill'` / `'run_autofill'`.

- [ ] **Step 3: Implement**

In `bot/handlers/commands.py` replace `cmd_autofill` with:

```python
async def run_autofill(bot, *, chat_id: int, topic_id: int, tg_user_id: int) -> str:
    """Fill Окружение/Классификация/Роль for the topic's ticket; HTML result text."""
    context = await get_operator_topic_context(
        telegram_user_id=tg_user_id, topic_id=topic_id, chat_id=chat_id,
    )
    ticket_id = context.record.ticket_id
    if not operators.ai_enabled_for(chat_id=chat_id):
        return _format_autofill_result(AutofillResult(updated=False, error=operators.AI_OFF_NOTE))
    client = HDEApiClient()
    info = await client.get_ticket_info(ticket_id)
    posts = await client.get_ticket_posts(ticket_id)
    try:
        comments = await client.get_ticket_comments(ticket_id)
    except HDEApiError:
        comments = []
    all_posts = sorted(posts + comments, key=post_sort_key)
    history = _build_history_text(all_posts, info)
    result = await apply_ticket_fields(
        bot, ticket_id, chat_id, context.topic_id, history,
        ticket_title=context.record.ticket_name or "",
        posts=all_posts,
    )
    return _format_autofill_result(result)


@router.message(Command("autofill"))
async def cmd_autofill(message: Message) -> None:
    await _run_operator_command(message, lambda: run_autofill(
        message.bot, chat_id=message.chat.id,
        topic_id=message.message_thread_id, tg_user_id=message.from_user.id,
    ))
```

In `bot/handlers/ai_feedback.py` replace `suggest_button_kb` with:

```python
def suggest_button_kb() -> InlineKeyboardMarkup:
    """Keyboard attached to client-reply messages in the topic (Phase 3)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💡 Предложить ответ", callback_data="ai:suggest"),
        InlineKeyboardButton(text="🪄 Автополя", callback_data="tp:autofill"),
    ]])
```

Append to `bot/handlers/topic_actions.py`:

```python
@router.callback_query(F.data == "tp:autofill")
async def cb_autofill(callback: CallbackQuery) -> None:
    from . import commands   # commands includes this router: import at call time, not at module load

    await callback.answer("Заполняю поля…")
    msg = callback.message
    try:
        result = await commands.run_autofill(
            callback.bot, chat_id=msg.chat.id,
            topic_id=msg.message_thread_id, tg_user_id=callback.from_user.id,
        )
    except Exception as exc:
        if not isinstance(exc, OperatorReplyError):
            logger.exception("topic_actions: autofill failed")
        result = format_operator_exception(exc)
    await msg.answer(result, parse_mode="HTML")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_topic_actions.py tests/test_env_autofill.py tests/test_priority_type_autofill.py -q`
Expected: all PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/handlers/commands.py bot/handlers/ai_feedback.py bot/handlers/topic_actions.py tests/test_topic_actions.py
git commit -m "feat(topics): 🪄 Автополя button next to 💡 under the latest client message"
```

---

### Task 7: Deploy stage 2 and check it live

**Files:** none changed.

- [ ] **Step 1: Push and deploy**

```bash
git push origin main
ssh config1 "cd /opt/hde-bot && sudo -n git pull --ff-only && sudo -n systemctl restart hde-bot && sleep 5 && systemctl is-active hde-bot"
```
Expected: `active`.

- [ ] **Step 2: Check the log**

```bash
ssh config1 "sudo -n tail -n 80 /opt/hde-bot/logs/bot.log | grep -E 'ERROR|Traceback' | tail -20"
```
Expected: nothing after the restart time.

- [ ] **Step 3: Owner live check** (ask via os-step-by-step): in a topic of their own open ticket write «тест кнопок, не для клиента» → «💬 Комментарий» → the internal comment appears in HDE → «🗑 Удалить из HDE» → «Да, удалить» → the comment is gone in HDE. Then check `bot.log` for `topic_actions` errors. No public «📤 Клиенту» test without separate consent.

- [ ] **Step 4: Record**: `record_work` in the `om` vault; os-done-or-not report.
